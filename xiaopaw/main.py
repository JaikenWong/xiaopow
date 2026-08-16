"""XiaoPaw 进程入口

启动顺序：
1. 加载 config.yaml（飞书配置、agent 参数、sandbox 配置等）
2. 初始化日志 + Prometheus metrics 服务
3. 初始化 SessionManager、CleanupService、CronService
4. 写入飞书凭证到沙盒 workspace/.config/feishu.json（凭证不经过 LLM）
5. 启动 CleanupService.sweep()（清理历史残留文件）
6. 构建真实 agent_fn（使用 build_agent_fn 工厂）
7. 启动 FeishuListener（WebSocket）+ metrics 服务 + 可选 TestAPI
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

import yaml
from lark_oapi.client import Client, LogLevel

from xiaopaw.agents.main_crew import build_agent_fn
from xiaopaw.cleanup.service import CleanupService
from xiaopaw.cron.service import CronService
from xiaopaw.feishu.downloader import FeishuDownloader
from xiaopaw.feishu.listener import FeishuListener, run_forever
from xiaopaw.feishu.sender import FeishuSender
from xiaopaw.observability.logging_config import setup_logging
from xiaopaw.observability.metrics_server import start_metrics_server
from xiaopaw.runner import Runner
from xiaopaw.session.manager import SessionManager

logger = logging.getLogger(__name__)


def _disable_crewai_tracing_prompts() -> None:
    """关闭 CrewAI trace 交互提示。

    新版 CrewAI 在 kickoff() 后可能弹出
    "Would you like to view your execution traces? [y/N] (20s timeout)"
    该提示用 input() 阻塞等待键盘输入（前台终端 20s，无人应答），
    会卡住 per-routing_key worker 队列。守护进程场景必须禁用。
    SDK 环境变量 CREWAI_TRACING_ENABLED=false 显式关闭 tracing，
    同时调用 set_suppress_tracing_messages 抑制首次执行确认/提示。
    两者都做，防御 CrewAI 版本差异。
    """
    os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
    try:
        from crewai.events.listeners.tracing.utils import (  # noqa: PLC0415
            set_suppress_tracing_messages,
        )

        set_suppress_tracing_messages(True)
    except ImportError:
        # 旧版 CrewAI 无此 API，环境变量已兜底
        logger.debug("crewai tracing utils not available, skipped")


def _load_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. 请先复制 config.yaml.template 并填写配置。"
        )
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return data


async def _daily_cleanup_loop(cleanup_svc: CleanupService) -> None:
    """每日 3:00（Asia/Shanghai）定时清理（独立协程，不依赖 CronService）。"""
    import datetime
    import zoneinfo

    _TZ = zoneinfo.ZoneInfo("Asia/Shanghai")

    while True:
        now = datetime.datetime.now(_TZ)
        next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run += datetime.timedelta(days=1)
        sleep_s = (next_run - now).total_seconds()
        await asyncio.sleep(sleep_s)
        try:
            await cleanup_svc.sweep()
        except Exception:  # noqa: BLE001
            logger.warning("cleanup: daily sweep failed", exc_info=True)


async def _run_services(
    cfg: dict,
    stop_evt: asyncio.Event,
    reload_evt: asyncio.Event,
    data_dir: Path,
    feishu_stop_evt: asyncio.Event | None = None,
    status: dict | None = None,
) -> bool:
    """构建并运行核心服务，直到 stop/reload/feishu 断开事件触发。

    Returns:
        True 表示配置变更请求热重启，False 表示收到退出信号。
    """
    if status is None:
        status = {}
    # 注意：保留入参 status 引用，使外部 dict 与本函数内共享同一对象。
    # 若用 `status = status or {}` 在 status={} 时会被替换为新 dict，导致
    # 调用方 status["feishu_connected"] 仍然拿不到更新。

    # ── 0. 飞书断开状态检查（用户先前已通过 UI 断开） ────────────────────────
    if feishu_stop_evt is not None and feishu_stop_evt.is_set():
        logger.info("feishu: previously disconnected by user, skipping listener")
        status["feishu_connected"] = False
        # 不启动 listener，仅等待 stop/reload
        stop_waiter = asyncio.create_task(stop_evt.wait(), name="stop-waiter")
        reload_waiter = asyncio.create_task(reload_evt.wait(), name="reload-waiter")
        done, pending = await asyncio.wait(
            {stop_waiter, reload_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return reload_evt.is_set()

    status["feishu_connected"] = True
    # ── 1. 读取关键配置 ────────────────────────────────────────────────────
    feishu_cfg = cfg.get("feishu", {})
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    if not app_id or not app_secret:
        raise RuntimeError(
            "feishu.app_id / feishu.app_secret 不能为空，请检查 config.yaml"
        )

    # 💡 模型配置：支持通用 OpenAI 兼容端点（base_url/api_key 可配，留空走默认 MiniMax）
    agent_cfg = cfg.get("agent", {})
    model = agent_cfg.get("model", "MiniMax-M3")
    base_url = agent_cfg.get("base_url", "") or ""
    api_key = agent_cfg.get("api_key", "") or ""
    sub_agent_model = agent_cfg.get("sub_agent_model", "MiniMax-M3")

    # host 型 Skill（claude_code）在宿主机执行的配置段
    claude_cfg = cfg.get("claude", {}) or {}

    max_history_turns = cfg.get("session", {}).get("max_history_turns", 20)
    sandbox_url = cfg.get("sandbox", {}).get("url", "http://localhost:8022/mcp")

    debug_cfg = cfg.get("debug", {})
    enable_test_api = debug_cfg.get("enable_test_api", False)
    test_api_host = debug_cfg.get("test_api_host", "127.0.0.1")
    test_api_port = debug_cfg.get("test_api_port", 9090)

    runner_cfg = cfg.get("runner", {})
    idle_timeout = runner_cfg.get("queue_idle_timeout_s", 300.0)

    # ── 2. 构建 Feishu HTTP Client ─────────────────────────────────────────
    client = (
        Client.builder()
        .app_id(app_id)
        .app_secret(app_secret)
        .log_level(LogLevel.INFO)
        .build()
    )

    # ── 3. 初始化核心服务 ───────────────────────────────────────────────────
    session_mgr = SessionManager(data_dir=data_dir)
    sender = FeishuSender(client=client)
    downloader = FeishuDownloader(client=client, data_dir=data_dir)
    cleanup_svc = CleanupService(data_dir=data_dir)

    # 写入飞书凭证到沙盒 .config 目录（凭证不经过 LLM）
    cleanup_svc.write_feishu_credentials(app_id=app_id, app_secret=app_secret)

    # 写入百度千帆 API Key 到沙盒 .config 目录（支持 baidu_search Skill）
    baidu_api_key = cfg.get("baidu", {}).get("api_key", "") or os.environ.get("BAIDU_API_KEY", "")
    cleanup_svc.write_baidu_credentials(api_key=baidu_api_key)

    # 启动时执行一次存储清理（清除历史残留）
    try:
        await cleanup_svc.sweep()
    except Exception:  # noqa: BLE001
        logger.warning("cleanup: startup sweep failed", exc_info=True)

    # ── 4. 构建真实 agent_fn ────────────────────────────────────────────────
    agent_fn = build_agent_fn(
        sender=sender,
        max_history_turns=max_history_turns,
        sandbox_url=sandbox_url,
        model=model,
        base_url=base_url,
        api_key=api_key,
        sub_agent_model=sub_agent_model,
        claude_cfg=claude_cfg,
    )

    # ── 5. 构建 Runner ──────────────────────────────────────────────────────
    runner = Runner(
        session_mgr=session_mgr,
        sender=sender,
        agent_fn=agent_fn,
        downloader=downloader,
        idle_timeout=idle_timeout,
    )

    # ── 6. CronService ──────────────────────────────────────────────────────
    (data_dir / "cron").mkdir(parents=True, exist_ok=True)
    cron_svc = CronService(data_dir=data_dir, dispatch_fn=runner.dispatch)
    await cron_svc.start()

    # ── 7. WebSocket Listener ───────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    allowed_chats: list[str] = feishu_cfg.get("allowed_chats", []) or []
    listener = FeishuListener(
        app_id=app_id,
        app_secret=app_secret,
        on_message=runner.dispatch,
        loop=loop,
        allowed_chats=allowed_chats if allowed_chats else None,
        on_bot_added=None,
    )

    logger.info("XiaoPaw ready. sandbox_url=%s, test_api=%s", sandbox_url, enable_test_api)

    # ── 8. 并行启动所有服务 ─────────────────────────────────────────────────
    feishu_task = asyncio.create_task(run_forever(listener), name="feishu-listener")

    # WS 任务死亡（异常/未捕获错误）→ 标记断开，便于 UI 显示真实状态
    def _on_feishu_done(task: asyncio.Task) -> None:
        try:
            exc = task.exception()
            if exc is not None:
                logger.warning("feishu: listener task died: %s", exc)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        status["feishu_connected"] = False

    feishu_task.add_done_callback(_on_feishu_done)

    tasks = [
        feishu_task,
        asyncio.create_task(
            start_metrics_server(host="127.0.0.1", port=9100),
            name="metrics-server",
        ),
        asyncio.create_task(
            _daily_cleanup_loop(cleanup_svc),
            name="cleanup-scheduler",
        ),
    ]

    # ── 8.5 事件 waiter（提前创建，供异常清理路径统一 cancel）────────────────
    stop_waiter = asyncio.create_task(stop_evt.wait(), name="stop-waiter")
    reload_waiter = asyncio.create_task(reload_evt.wait(), name="reload-waiter")
    waiters = [stop_waiter, reload_waiter]
    if feishu_stop_evt is not None:
        feishu_stop_waiter = asyncio.create_task(
            feishu_stop_evt.wait(), name="feishu-stop-waiter"
        )
        waiters.append(feishu_stop_waiter)

    try:
        if enable_test_api:
            from xiaopaw.api.test_server import create_test_app  # noqa: PLC0415

            test_app = create_test_app(runner=runner, session_mgr=session_mgr)
            tasks.append(
                asyncio.create_task(
                    _run_test_api(test_app, host=test_api_host, port=test_api_port),
                    name="test-api",
                )
            )
            logger.info("TestAPI enabled: http://%s:%d", test_api_host, test_api_port)

        # ── 9. 等待退出信号或配置热重启 ─────────────────────────────────────
        done, pending = await asyncio.wait(
            [*tasks, *waiters],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # 服务 task 异常退出时必须可见：否则表现为"正常 shutdown"静默退出整个进程
        service_exc: BaseException | None = None
        for t in done:
            if t in waiters:
                continue
            exc = t.exception()
            if exc is not None:
                logger.error("服务 %s 异常退出", t.get_name(), exc_info=exc)
                service_exc = exc

        reload_requested = reload_evt.is_set()

        # 处理飞书断开事件：仅取消 listener，让 cron/metrics/清理继续运行
        feishu_disconnect_requested = (
            feishu_stop_evt is not None and feishu_stop_evt.is_set()
        )
        if feishu_disconnect_requested:
            status["feishu_connected"] = False
            logger.info("feishu: disconnect requested, cancelling listener only")

        # 取消所有仍在运行的任务（含服务 task 与未触发的 waiter）
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if service_exc is not None:
            raise service_exc
    except BaseException:
        # 启动/运行中途失败：cancel 已创建的任务再抛出，防止热重启循环中泄漏重复实例
        for t in [*tasks, *waiters]:
            t.cancel()
        await asyncio.gather(*tasks, *waiters, return_exceptions=True)
        raise

    logger.info(
        "XiaoPaw services stopped. reason=%s",
        "config-reload" if reload_requested else "shutdown",
    )
    return reload_requested


_DEFAULT_CONFIG: dict = {
    "workspace": {"id": "xiaopaw-default", "name": "XiaoPaw 工作助手"},
    "feishu": {
        "app_id": "",
        "app_secret": "",
        "encrypt_key": "",
        "verification_token": "",
    },
    "bot": {"loading_message": "思考中...", "prefix": ""},
    "agent": {
        "model": "MiniMax-M3",
        "base_url": "",
        "api_key": "",
        "max_iter": 50,
        "max_input_tokens": 30000,
        "sub_agent_model": "MiniMax-M3",
        "sub_agent_max_iter": 20,
        "timeout_s": 300,
    },
    "claude": {
        "workspace_path": "",
        "model": "",
        "timeout": 600,
        "max_output_chars": 12000,
    },
    "skills": {"global_dir": "../skills", "local_dir": "./skills"},
    "sandbox": {
        "url": "http://localhost:8022/mcp",
        "workspace_dir": "/workspace",
        "timeout_s": 120,
        "max_retries": 2,
    },
    "session": {"max_history_turns": 20},
    "runner": {"queue_idle_timeout_s": 300, "max_queue_size": 10},
    "sender": {"max_retries": 3, "retry_backoff": [1, 2, 4]},
    "data_dir": "./data",
    "debug": {
        "enable_test_api": False,
        "test_api_port": 9090,
        "test_api_host": "127.0.0.1",
    },
}


def _ensure_config(config_path: Path, is_desktop: bool) -> None:
    """确保配置文件存在。

    CLI 模式：缺失即报错（保持原行为）。
    桌面模式：首次运行生成默认配置模板，供前端配置窗口填写。
    """
    if config_path.exists():
        return
    if not is_desktop:
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. 请先复制 config.yaml.template 并填写配置。"
        )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    tmp = config_path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(_DEFAULT_CONFIG, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.replace(tmp, config_path)
    logger.info("desktop: default config written to %s", config_path)


def _resolve_data_dir(cfg: dict, config_path: Path) -> Path:
    """解析 data_dir。

    相对路径时相对于 config.yaml 所在目录解析（而非进程 cwd）。
    开发模式：config 在 repo_root → data_dir=repo_root/data（与原来一致）
    桌面模式：config 在 app_data_dir → data_dir=app_data_dir/data（可写）
    避免 sidecar 的 cwd 是 app bundle（只读）或 "/" 导致写入失败。
    """
    raw = cfg.get("data_dir", "./data")
    p = Path(raw)
    if p.is_absolute():
        return p
    return (config_path.parent / raw).resolve()


async def async_main(args: argparse.Namespace) -> None:
    config_path = args.config_path.resolve()
    is_desktop = bool(args.api_port and args.api_port > 0)
    _ensure_config(config_path, is_desktop)
    cfg = _load_config(config_path)

    # ── 日志初始化（仅一次，不随配置热重启重建）─────────────────────────────
    data_dir = _resolve_data_dir(cfg, config_path)
    setup_logging(data_dir / "logs")
    logger.info("XiaoPaw starting. data_dir=%s", data_dir)

    # ── 事件：stop = 退出进程；reload = 配置变更热重启；feishu_stop = UI 断开 WS ─
    stop_evt = asyncio.Event()
    reload_evt = asyncio.Event()
    feishu_stop_evt = asyncio.Event()

    # ── 信号处理：SIGINT/SIGTERM 优雅退出（桌面 sidecar 生命周期必需）───────
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_evt.set)
        except NotImplementedError:
            # 某些平台（如 Windows ProactorLoop）不支持 add_signal_handler
            logger.warning("signal handler not supported for %s", sig)

    # ── 配置管理服务（仅桌面模式启用，常驻，跨热重启存活）────────────────────
    status: dict = {"running": False, "reloads": 0, "error": None,
                    "feishu_connected": False}
    config_runner = None
    pid_path: Path | None = None
    if is_desktop:
        from xiaopaw.api.config_server import start_config_server  # noqa: PLC0415

        log_path = data_dir / "logs" / "xiaopaw.log"
        config_runner = await start_config_server(
            config_path=config_path,
            reload_event=reload_evt,
            status=status,
            host=args.api_host,
            port=args.api_port,
            log_path=log_path,
            feishu_stop_evt=feishu_stop_evt,
        )

        # 写 pid 文件：Tauri 外壳复用残留 sidecar 时，退出可据此 kill，
        # 避免僵尸进程长期占用 9191 端口
        pid_path = config_path.parent / "xiaopaw.pid"
        try:
            pid_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            logger.warning("desktop: failed to write pid file %s", pid_path)

    try:
        while True:
            cfg = _load_config(config_path)
            status["running"] = True
            status["error"] = None
            try:
                should_reload = await _run_services(
                    cfg, stop_evt, reload_evt, data_dir,
                    feishu_stop_evt=feishu_stop_evt,
                    status=status,
                )
            except Exception as exc:  # noqa: BLE001
                # 配置缺失/服务启动失败（如飞书未配置）：不崩溃，
                # 进入"等待配置"状态，等前端 PUT 配置后热重启
                logger.error("services failed to run: %s", exc, exc_info=True)
                status["running"] = False
                status["error"] = str(exc)

                # 等待配置变更或退出信号
                stop_waiter = asyncio.create_task(stop_evt.wait())
                reload_waiter = asyncio.create_task(reload_evt.wait())
                await asyncio.wait(
                    {stop_waiter, reload_waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                stop_waiter.cancel()
                reload_waiter.cancel()
                await asyncio.gather(stop_waiter, reload_waiter, return_exceptions=True)
                if stop_evt.is_set():
                    break
                reload_evt.clear()
                continue

            status["running"] = False
            status["error"] = None

            if not should_reload:
                break
            reload_evt.clear()
            logger.info("config reload requested, restarting services")
    finally:
        if config_runner is not None:
            await config_runner.cleanup()
        if pid_path is not None:
            with contextlib.suppress(OSError):
                pid_path.unlink(missing_ok=True)

    logger.info("XiaoPaw exited.")


async def _run_test_api(app: object, host: str, port: int) -> None:
    """启动 aiohttp Test API Server。"""
    from aiohttp import web  # noqa: PLC0415

    app_runner = web.AppRunner(app)
    await app_runner.setup()
    site = web.TCPSite(app_runner, host=host, port=port)
    await site.start()
    logger.info("TestAPI listening on http://%s:%d", host, port)
    try:
        await asyncio.Event().wait()
    finally:
        await app_runner.cleanup()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="XiaoPaw 桌面工作助手")
    parser.add_argument(
        "--config",
        dest="config_path",
        type=Path,
        default=None,
        help="config.yaml 路径（默认 <repo_root>/config.yaml）",
    )
    parser.add_argument(
        "--api-host",
        default="127.0.0.1",
        help="配置管理服务监听地址（默认 127.0.0.1）",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=0,
        help="配置管理服务端口，0 表示禁用（桌面 sidecar 模式必须 > 0）",
    )
    args = parser.parse_args(argv)

    if args.config_path is None:
        repo_root = Path(__file__).resolve().parents[1]
        args.config_path = repo_root / "config.yaml"
    return args


def main(argv: list[str] | None = None) -> None:
    _disable_crewai_tracing_prompts()
    args = _parse_args(argv)
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
