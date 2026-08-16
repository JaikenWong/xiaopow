"""ConfigServer — 桌面应用配置管理 HTTP 服务

供 Tauri 前端读写 config.yaml，并触发服务热重启。

设计要点：
- 只监听 127.0.0.1（本地回环），不对外暴露
- Host 头校验 + Origin 白名单：防止恶意网页跨站 PUT 改写配置（CORS 劫持 / DNS rebinding）
- GET /api/config 返回脱敏后的配置（app_secret/api_key 等以 "******" 占位）
- PUT /api/config 写入完整配置；字段值为 "******" 时保留旧值（不覆盖真实密钥）
- PUT 成功后触发 reload_event，主进程重启服务（listener/cron 等），配置服务自身常驻

字段脱敏规则：任何值为敏感语义的 key（secret/key/token 结尾）都会被掩码，
避免密钥经 HTTP 明文回传（即使只在本机回环）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp
import yaml
from aiohttp import web

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────────────────

_MASK = "******"

# 敏感 key 的后缀/关键词（不区分大小写）
_SENSITIVE_HINTS = ("secret", "key", "token", "password", "credential")

_config_path_key = web.AppKey("config_path", Path)
_reload_event_key = web.AppKey("reload_event", asyncio.Event)
_feishu_stop_evt_key = web.AppKey("feishu_stop_evt", asyncio.Event)
_status_key = web.AppKey("status", dict)
_log_path_key = web.AppKey("log_path", Path)


# ── 脱敏工具 ───────────────────────────────────────────────────────────────


def _is_sensitive_key(key: str) -> bool:
    """判断配置 key 是否敏感（含 secret/key/token 等）。"""
    k = key.lower()
    return any(h in k for h in _SENSITIVE_HINTS)


def mask_config(cfg: dict) -> dict:
    """深拷贝配置并掩码敏感字段值。

    只处理字符串值；非字符串（数字/列表/嵌套 dict）递归处理。
    """
    masked = copy.deepcopy(cfg)
    _mask_inplace(masked)
    return masked


def _mask_inplace(node: Any) -> None:
    if isinstance(node, dict):
        for k, v in list(node.items()):
            if isinstance(v, dict):
                _mask_inplace(v)
            elif isinstance(v, str) and v and _is_sensitive_key(k):
                node[k] = _MASK
    elif isinstance(node, list):
        for item in node:
            _mask_inplace(item)


def _restore_masked(new_cfg: dict, old_cfg: dict) -> dict:
    """写入前：若新配置中某敏感字段为掩码占位符，则用旧值回填。

    防止前端把 GET 到的 "******" 原样写回，覆盖真实密钥。
    """
    merged = copy.deepcopy(new_cfg)
    _restore_inplace(merged, old_cfg)
    return merged


def _restore_inplace(new_node: Any, old_node: Any) -> None:
    if isinstance(new_node, dict) and isinstance(old_node, dict):
        for k, v in new_node.items():
            if isinstance(v, dict) and isinstance(old_node.get(k), dict):
                _restore_inplace(v, old_node[k])
            elif v == _MASK and k in old_node:
                new_node[k] = old_node[k]
    elif isinstance(new_node, list) and isinstance(old_node, list):
        for i, item in enumerate(new_node):
            if i < len(old_node):
                _restore_inplace(item, old_node[i])


# ── 处理器 ──────────────────────────────────────────────────────────────────


async def _handle_health(request: web.Request) -> web.Response:
    """GET /api/health — 探活。"""
    status = request.app[_status_key]
    return web.json_response({"ok": True, "running": status.get("running", False)})


async def _handle_get_config(request: web.Request) -> web.Response:
    """GET /api/config — 返回脱敏后的完整配置。"""
    config_path: Path = request.app[_config_path_key]
    cfg = _read_yaml(config_path)
    return web.json_response({"config": mask_config(cfg)})


async def _handle_put_config(request: web.Request) -> web.Response:
    """PUT /api/config — 写入配置并触发服务重启。

    Body: {"config": {...}}，config 为完整配置对象。
    值为 "******" 的敏感字段保留旧值。
    """
    config_path: Path = request.app[_config_path_key]
    reload_event: asyncio.Event = request.app[_reload_event_key]
    status: dict = request.app[_status_key]

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    new_cfg = body.get("config") if isinstance(body, dict) else None
    if not isinstance(new_cfg, dict):
        return web.json_response({"error": "config 必须是对象"}, status=422)

    old_cfg = _read_yaml(config_path)
    merged = _restore_masked(new_cfg, old_cfg)

    try:
        _write_yaml(config_path, merged)
    except OSError as exc:
        logger.exception("config_server: failed to write config")
        return web.json_response({"error": f"写入失败: {exc}"}, status=500)

    # 触发热重启（主进程监听该事件后重建服务）
    reload_event.set()
    status["reloads"] = status.get("reloads", 0) + 1
    logger.info("config_server: config updated, reload triggered")
    return web.json_response({"ok": True, "reloaded": True})


async def _handle_get_status(request: web.Request) -> web.Response:
    """GET /api/status — 运行状态。"""
    status: dict = request.app[_status_key]
    config_path: Path = request.app[_config_path_key]
    return web.json_response(
        {
            "running": status.get("running", False),
            "reloads": status.get("reloads", 0),
            "error": status.get("error"),
            "feishu_connected": status.get("feishu_connected", False),
            "config_path": str(config_path),
        }
    )


async def _handle_get_logs(request: web.Request) -> web.Response:
    """GET /api/logs?lines=100 — 日志文件尾部。"""
    log_path: Path | None = request.app.get(_log_path_key)
    if log_path is None or not log_path.exists():
        return web.json_response({"lines": []})

    try:
        lines = int(request.query.get("lines", "100"))
    except ValueError:
        lines = 100
    lines = max(1, min(2000, lines))

    # tail：只读文件尾部有限字节（避免大日志整文件载入内存）。
    # 假设平均行长 ~256 字节，再加 8KB 冗余，从文件尾 seek 后一次读出。
    import os

    with open(log_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - (lines * 256 + 8192)))
        data = f.read()
    tail = data.decode("utf-8", errors="replace").splitlines()[-lines:]
    return web.json_response({"lines": tail})


async def _handle_disconnect_feishu(request: web.Request) -> web.Response:
    """POST /api/disconnect-feishu — 断开飞书机器人 WebSocket 连接。

    置位 feishu_stop_evt，主进程 _run_services 监听到后仅取消 listener task
    （cron/metrics/清理等继续运行）；需调用 /api/reconnect-feishu 才能重新连接。
    """
    status: dict = request.app[_status_key]
    evt = request.app.get(_feishu_stop_evt_key)
    if evt is None:
        return web.json_response({"ok": False, "error": "未启用飞书断开控制"}, status=400)
    evt.set()
    status["feishu_connected"] = False
    logger.info("config_server: feishu disconnect requested")
    return web.json_response({"ok": True, "disconnected": True})


async def _handle_reconnect_feishu(request: web.Request) -> web.Response:
    """POST /api/reconnect-feishu — 重新连接飞书机器人 WebSocket。

    清空 feishu_stop_evt，触发 reload_event 让主进程重建 listener task。
    """
    reload_event: asyncio.Event = request.app[_reload_event_key]
    evt = request.app.get(_feishu_stop_evt_key)
    if evt is None:
        return web.json_response({"ok": False, "error": "未启用飞书断开控制"}, status=400)
    evt.clear()
    reload_event.set()
    status: dict = request.app[_status_key]
    status["reloads"] = status.get("reloads", 0) + 1
    logger.info("config_server: feishu reconnect requested")
    return web.json_response({"ok": True, "reconnecting": True})


async def _handle_test_llm(request: web.Request) -> web.Response:
    """POST /api/test-llm — 测试大模型连通性。

    用当前配置的 model/base_url/api_key 发一条最小 chat completion 请求。
    api_key 从磁盘配置或环境变量读取，绝不回传。响应只含 ok/model/latency/error。
    """
    config_path: Path = request.app[_config_path_key]
    cfg = _read_yaml(config_path)
    agent_cfg = cfg.get("agent", {}) or {}

    model = (agent_cfg.get("model") or "").strip() or "MiniMax-M3"
    base_url = (agent_cfg.get("base_url") or "").strip().rstrip("/") or "https://api.minimaxi.com/v1"
    api_key = (agent_cfg.get("api_key") or "").strip() or os.environ.get("MINIMAX_API_KEY", "")

    if not api_key:
        return web.json_response(
            {"ok": False, "error": "未配置 agent.api_key，且环境变量 MINIMAX_API_KEY 为空"},
            status=400,
        )

    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    try:
        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        return web.json_response({"ok": False, "model": model, "error": f"请求失败: {exc}"}, status=502)

    if resp.status != 200:
        msg = ""
        if isinstance(body, dict):
            err = body.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(body.get("error"))
        return web.json_response(
            {"ok": False, "model": model, "error": msg or f"HTTP {resp.status}"},
            status=502,
        )

    return web.json_response({"ok": True, "model": model, "latency_ms": latency_ms})


# 飞书开放平台 tenant_access_token 接口（直连探测凭证有效性）
_FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"


async def _handle_test_feishu(request: web.Request) -> web.Response:
    """POST /api/test-feishu — 测试飞书机器人凭证连通性。

    用当前 feishu.app_id/app_secret 调一次 tenant_access_token 接口
    验证凭证有效性 + 网络可达性。响应不含 token 任何字段（仅 ok/code/expire/error）。
    """
    config_path: Path = request.app[_config_path_key]
    cfg = _read_yaml(config_path)
    feishu_cfg = cfg.get("feishu", {}) or {}
    app_id = (feishu_cfg.get("app_id") or "").strip()
    app_secret = (feishu_cfg.get("app_secret") or "").strip()

    if not app_id or not app_secret:
        return web.json_response(
            {"ok": False, "error": "feishu.app_id / feishu.app_secret 未配置"},
            status=400,
        )

    payload = {"app_id": app_id, "app_secret": app_secret}
    headers = {"Content-Type": "application/json"}

    try:
        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _FEISHU_TOKEN_URL,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json()
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
    except aiohttp.ClientError as exc:
        return web.json_response(
            {"ok": False, "error": f"网络错误: {exc}"}, status=502
        )
    except asyncio.TimeoutError:
        return web.json_response({"ok": False, "error": "请求超时"}, status=502)
    except (ValueError, KeyError) as exc:
        return web.json_response(
            {"ok": False, "error": f"响应解析失败: {exc}"}, status=502
        )

    code = body.get("code") if isinstance(body, dict) else None
    if code != 0:
        msg = body.get("msg", "未知错误") if isinstance(body, dict) else "未知错误"
        return web.json_response(
            {"ok": False, "code": code, "error": msg},
            status=502,
        )

    expire = body.get("expire", 0) if isinstance(body, dict) else 0
    return web.json_response(
        {"ok": True, "code": code, "expire": expire, "latency_ms": latency_ms}
    )


# ── YAML 读写 ───────────────────────────────────────────────────────────────


def _read_yaml(config_path: Path) -> dict:
    if config_path.exists():
        try:
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            logger.exception("config_server: failed to parse config, return empty")
            return {}
    return {}


def _write_yaml(config_path: Path, cfg: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # 原子写：rename 覆盖
    import os

    os.replace(tmp, config_path)


# ── CORS + Host 校验 ────────────────────────────────────────────────────────
#
# 前端从 tauri://localhost（macOS/Linux 打包后）或 http://tauri.localhost
# （Windows 打包后）或 file:// / http://localhost（调试）加载，通过 fetch
# 访问本机 127.0.0.1:9191 属跨源请求，需要 CORS 响应头。
#
# ⚠️ 安全：服务无鉴权，若放行任意 Origin，用户浏览器中任意恶意网页都
# 可直接 PUT /api/config 改写配置（如把 claude.workspace_path 指向任意
# 目录）。故：
# 1. Origin 白名单：只回显白名单内的 Origin（tauri 两个来源 + 本地调试 + file:// 的 null）
# 2. Host 头校验：只接受 127.0.0.1/localhost/[::1]，防 DNS rebinding
#    （恶意域名解析到回环地址，Origin 是攻击者自己的域名）

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

# 允许的 Origin：Tauri WebView（打包）、file:// 调试（Origin: null）、
# 本地 dev server（http://localhost:* / http://127.0.0.1:*）
_ALLOWED_ORIGIN_EXACT = {"tauri://localhost", "http://tauri.localhost", "null"}
_ALLOWED_ORIGIN_PREFIXES = ("http://localhost:", "http://127.0.0.1:")


def _origin_allowed(origin: str) -> bool:
    """判断 Origin 是否在白名单内。"""
    if origin in _ALLOWED_ORIGIN_EXACT:
        return True
    return origin.startswith(_ALLOWED_ORIGIN_PREFIXES)


def _host_allowed(host_header: str) -> bool:
    """判断 Host 头是否为本机回环地址（防 DNS rebinding）。"""
    if not host_header:
        return False
    hostname = host_header.rsplit(":", 1)[0].lower()
    return hostname in _ALLOWED_HOSTS


def _add_cors_headers(resp: web.StreamResponse, origin: str | None) -> None:
    # 只回显白名单内 Origin；非浏览器请求（curl / 本地脚本，无 Origin 头）不加 ACAO
    if origin:
        resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, PUT, POST, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"


@web.middleware
async def _cors_middleware(request: web.Request, handler):
    # Host 校验：非本机回环 Host 一律拒绝
    if not _host_allowed(request.headers.get("Host", "")):
        logger.warning("config_server: rejected bad Host header: %r", request.headers.get("Host"))
        return web.json_response({"error": "forbidden host"}, status=403)

    # Origin 校验：非白名单来源的浏览器请求直接拒绝（无 Origin 视为非浏览器请求放行）
    origin = request.headers.get("Origin")
    if origin is not None and not _origin_allowed(origin):
        logger.warning("config_server: rejected bad Origin: %r", origin)
        return web.json_response({"error": "forbidden origin"}, status=403)

    # 预检请求（PUT 带 Content-Type: application/json 会触发）直接放行
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
        _add_cors_headers(resp, origin)
        return resp
    resp = await handler(request)
    _add_cors_headers(resp, origin)
    return resp


# ── 应用工厂 ────────────────────────────────────────────────────────────────


def create_config_app(
    config_path: Path,
    reload_event: asyncio.Event,
    status: dict,
    log_path: Path | None = None,
    feishu_stop_evt: asyncio.Event | None = None,
) -> web.Application:
    """创建配置管理 aiohttp 应用。"""
    app = web.Application(middlewares=[_cors_middleware])
    app[_config_path_key] = config_path
    app[_reload_event_key] = reload_event
    app[_status_key] = status
    if log_path is not None:
        app[_log_path_key] = log_path
    if feishu_stop_evt is not None:
        app[_feishu_stop_evt_key] = feishu_stop_evt

    app.router.add_get("/api/health", _handle_health)
    app.router.add_get("/api/config", _handle_get_config)
    app.router.add_put("/api/config", _handle_put_config)
    app.router.add_get("/api/status", _handle_get_status)
    app.router.add_get("/api/logs", _handle_get_logs)
    app.router.add_post("/api/test-llm", _handle_test_llm)
    app.router.add_post("/api/test-feishu", _handle_test_feishu)
    app.router.add_post("/api/disconnect-feishu", _handle_disconnect_feishu)
    app.router.add_post("/api/reconnect-feishu", _handle_reconnect_feishu)
    return app


async def start_config_server(
    config_path: Path,
    reload_event: asyncio.Event,
    status: dict,
    host: str = "127.0.0.1",
    port: int = 9191,
    log_path: Path | None = None,
    feishu_stop_evt: asyncio.Event | None = None,
) -> web.AppRunner:
    """启动配置服务，返回 AppRunner（调用方负责 cleanup）。

    端口固定（前端 API_BASE 写死 9191），被占用时直接抛 OSError 快速失败：
    多实例场景由 Tauri 外壳的 pid 复用兜底（sidecar_already_running），
    此处不做静默向后探测——换端口会让前端全部请求落空，反而更难排查。
    """
    app = create_config_app(
        config_path, reload_event, status, log_path, feishu_stop_evt=feishu_stop_evt
    )
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host=host, port=port)
        await site.start()
    except OSError:
        await runner.cleanup()
        raise

    logger.info("ConfigServer listening on http://%s:%d", host, port)
    return runner
