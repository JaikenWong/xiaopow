"""xiaopaw/main.py 单元测试 — 聚焦：CLI 参数、配置加载、data_dir 解析、ensure_config、feishu 断开短路。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from xiaopaw import main as xmain


# ── _parse_args ─────────────────────────────────────────────────────────────


class TestParseArgs:
    def test_default_config_path(self):
        args = xmain._parse_args([])
        # 默认相对 __file__.resolve().parents[1] / config.yaml
        assert args.config_path.name == "config.yaml"
        assert args.api_host == "127.0.0.1"
        assert args.api_port == 0

    def test_explicit_config(self, tmp_path):
        cfg = tmp_path / "custom.yaml"
        args = xmain._parse_args(["--config", str(cfg)])
        assert args.config_path == cfg

    def test_api_port_desktop_mode(self):
        args = xmain._parse_args(["--api-port", "9191", "--api-host", "0.0.0.0"])
        assert args.api_port == 9191
        assert args.api_host == "0.0.0.0"


# ── _load_config ────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_loads_yaml(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(yaml.safe_dump({"foo": 1, "bar": [1, 2]}), encoding="utf-8")
        assert xmain._load_config(path) == {"foo": 1, "bar": [1, 2]}

    def test_empty_file_returns_empty_dict(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text("", encoding="utf-8")
        assert xmain._load_config(path) == {}

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            xmain._load_config(tmp_path / "missing.yaml")


# ── _resolve_data_dir ───────────────────────────────────────────────────────


class TestResolveDataDir:
    def test_relative_path(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        result = xmain._resolve_data_dir({"data_dir": "./data"}, cfg_path)
        assert result == (tmp_path / "data").resolve()

    def test_absolute_path_unchanged(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        abs_path = tmp_path / "absolute" / "data"
        result = xmain._resolve_data_dir({"data_dir": str(abs_path)}, cfg_path)
        assert result == abs_path

    def test_default_dot_data(self, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        result = xmain._resolve_data_dir({}, cfg_path)
        assert result == (tmp_path / "data").resolve()


# ── _ensure_config ──────────────────────────────────────────────────────────


class TestEnsureConfig:
    def test_cli_mode_raises_when_missing(self, tmp_path):
        path = tmp_path / "config.yaml"
        with pytest.raises(FileNotFoundError):
            xmain._ensure_config(path, is_desktop=False)

    def test_cli_mode_noop_when_exists(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("existing: 1\n", encoding="utf-8")
        xmain._ensure_config(path, is_desktop=False)  # 不应抛错

    def test_desktop_mode_writes_default(self, tmp_path, caplog):
        path = tmp_path / "config.yaml"
        xmain._ensure_config(path, is_desktop=True)
        assert path.exists()
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "feishu" in data
        assert "agent" in data


# ── _disable_crewai_tracing_prompts ─────────────────────────────────────────


class TestDisableCrewAITracing:
    def test_sets_env_var(self):
        import os

        # 清理之前可能残留的状态
        os.environ.pop("CREWAI_TRACING_ENABLED", None)
        xmain._disable_crewai_tracing_prompts()
        assert os.environ.get("CREWAI_TRACING_ENABLED") == "false"

    def test_does_not_overwrite_existing(self):
        import os

        os.environ["CREWAI_TRACING_ENABLED"] = "true"
        try:
            xmain._disable_crewai_tracing_prompts()
            assert os.environ["CREWAI_TRACING_ENABLED"] == "true"
        finally:
            os.environ.pop("CREWAI_TRACING_ENABLED", None)


# ── _run_services — feishu_stop 短路 ────────────────────────────────────────


class TestRunServicesFeishuShortCircuit:
    async def test_returns_reload_when_feishu_disconnected(self, tmp_path):
        """feishu_stop_evt 已置位时：不开 listener，仅等 stop/reload。"""
        import asyncio

        from xiaopaw.main import _run_services

        stop_evt = asyncio.Event()
        reload_evt = asyncio.Event()
        feishu_stop_evt = asyncio.Event()
        feishu_stop_evt.set()  # 已断开
        status: dict = {}

        cfg = {
            "feishu": {"app_id": "cli", "app_secret": "sec"},
            "agent": {"model": "x"},
        }

        # 触发 reload 来退出
        async def trigger_reload():
            await asyncio.sleep(0.01)
            reload_evt.set()

        trigger_task = asyncio.create_task(trigger_reload())
        try:
            result = await _run_services(
                cfg, stop_evt, reload_evt, tmp_path,
                feishu_stop_evt=feishu_stop_evt, status=status,
            )
        finally:
            trigger_task.cancel()

        assert result is True  # reload_requested
        assert status["feishu_connected"] is False


# ── _run_services — 缺凭证抛出 ─────────────────────────────────────────────


class TestRunServicesMissingCredentials:
    async def test_raises_when_app_id_missing(self, tmp_path):
        import asyncio

        from xiaopaw.main import _run_services

        stop_evt = asyncio.Event()
        reload_evt = asyncio.Event()
        cfg = {"feishu": {"app_id": "", "app_secret": ""}}

        with pytest.raises(RuntimeError, match="feishu.app_id"):
            await _run_services(
                cfg, stop_evt, reload_evt, tmp_path,
                feishu_stop_evt=None, status={},
            )


# ── main() / argparse ───────────────────────────────────────────────────────


def test_main_invokes_asyncio(monkeypatch):
    """main() 应调用 asyncio.run(async_main(args))。"""
    called = {}

    def fake_async_main(args):
        called["args"] = args  # 同步设置，便于断言

    monkeypatch.setattr(xmain, "async_main", fake_async_main)
    # 让 asyncio.run 直接同步调 fake_async_main（不真正进入事件循环）
    monkeypatch.setattr(
        xmain.asyncio, "run",
        lambda coro: fake_async_main(coro.cr_frame.f_locals.get("args"))
        if hasattr(coro, "cr_frame")
        else None,
    )

    xmain.main([])
    assert "args" in called


# ── _run_test_api ───────────────────────────────────────────────────────────


class TestRunTestApi:
    async def test_starts_and_stops_test_api_server(self):
        """验证 _run_test_api 能启动 web server 并清理资源（用超时强制退出）。"""
        import asyncio

        from aiohttp import web

        from xiaopaw.main import _run_test_api

        async def hello(request):
            return web.Response(text="hi")

        app = web.Application()
        app.router.add_get("/", hello)

        # _run_test_api 内部 await asyncio.Event().wait() 永不返回，
        # 用 asyncio.wait_for + TimeoutError 强制中断，触发 cleanup 路径
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                _run_test_api(app, host="127.0.0.1", port=0),
                timeout=0.5,
            )


# ── _daily_cleanup_loop（部分）──────────────────────────────────────────────


class TestDailyCleanupLoop:
    async def test_runs_sweep_when_triggered(self, monkeypatch):
        """验证 daily loop 会调用 cleanup_svc.sweep()。"""
        from unittest.mock import AsyncMock

        from xiaopaw.main import _daily_cleanup_loop

        cleanup_svc = AsyncMock()
        cleanup_svc.sweep = AsyncMock()

        # 第一次 sleep 正常返回 → 走到 sweep；第二次 sleep 抛 CancelledError 退出
        sleep_calls = 0

        async def fake_sleep(s):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError()

        monkeypatch.setattr("xiaopaw.main.asyncio.sleep", fake_sleep)

        with pytest.raises(asyncio.CancelledError):
            await _daily_cleanup_loop(cleanup_svc)
        # sweep 至少被调一次
        assert cleanup_svc.sweep.await_count >= 1

    async def test_swallows_sweep_errors(self, monkeypatch):
        """sweep 抛错时 loop 不应崩溃。"""
        from unittest.mock import AsyncMock

        from xiaopaw.main import _daily_cleanup_loop

        cleanup_svc = AsyncMock()
        cleanup_svc.sweep = AsyncMock(side_effect=RuntimeError("oops"))

        sleep_calls = 0

        async def fake_sleep(s):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 3:  # 第三次 sleep 时退出
                raise asyncio.CancelledError()

        monkeypatch.setattr("xiaopaw.main.asyncio.sleep", fake_sleep)

        with pytest.raises(asyncio.CancelledError):
            await _daily_cleanup_loop(cleanup_svc)
        # sweep 被调两次（第一次抛错被吞掉），循环继续
        assert cleanup_svc.sweep.await_count >= 2
