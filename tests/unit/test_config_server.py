"""ConfigServer 单元测试 — 脱敏、掩码回填、HTTP 处理器。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from xiaopaw.api import config_server
from xiaopaw.api.config_server import create_config_app, mask_config


@pytest.fixture
def sample_config() -> dict:
    return {
        "feishu": {"app_id": "cli_123", "app_secret": "secret_abc"},
        "agent": {"model": "MiniMax-M3", "api_key": "sk-xyz"},
        "baidu": {"api_key": "bce-v3/abc"},
        "claude": {"workspace_path": "/tmp/proj", "timeout": 600},
        "sandbox": {"url": "http://localhost:8022/mcp"},
    }


# ── 脱敏逻辑 ─────────────────────────────────────────────────────────────


class TestMaskConfig:
    def test_masks_sensitive_string_values(self, sample_config):
        masked = mask_config(sample_config)
        assert masked["feishu"]["app_secret"] == config_server._MASK
        assert masked["agent"]["api_key"] == config_server._MASK
        assert masked["baidu"]["api_key"] == config_server._MASK

    def test_keeps_non_sensitive(self, sample_config):
        masked = mask_config(sample_config)
        assert masked["feishu"]["app_id"] == "cli_123"
        assert masked["agent"]["model"] == "MiniMax-M3"
        assert masked["claude"]["workspace_path"] == "/tmp/proj"
        assert masked["claude"]["timeout"] == 600

    def test_does_not_mutate_original(self, sample_config):
        original = sample_config["feishu"]["app_secret"]
        mask_config(sample_config)
        assert sample_config["feishu"]["app_secret"] == original

    def test_masks_url_not_mistaken_as_secret(self):
        cfg = {"sandbox": {"url": "http://localhost:8022/mcp"}}
        masked = mask_config(cfg)
        assert masked["sandbox"]["url"] == "http://localhost:8022/mcp"


class TestRestoreMasked:
    def test_restores_masked_field_from_old(self):
        old = {"feishu": {"app_secret": "real_secret"}}
        new = {"feishu": {"app_secret": config_server._MASK}}
        merged = config_server._restore_masked(new, old)
        assert merged["feishu"]["app_secret"] == "real_secret"

    def test_keeps_newly_entered_value(self):
        old = {"feishu": {"app_secret": "old_secret"}}
        new = {"feishu": {"app_secret": "new_secret"}}
        merged = config_server._restore_masked(new, old)
        assert merged["feishu"]["app_secret"] == "new_secret"


# ── HTTP 处理器 ───────────────────────────────────────────────────────────


@pytest.fixture
def config_app(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    reload_event = asyncio.Event()
    status: dict = {"running": False, "reloads": 0, "error": None}
    return create_config_app(config_path, reload_event, status)


async def test_get_config_returns_masked(config_app, tmp_path):
    # 写入含密钥的配置
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path,
        {"feishu": {"app_id": "cli", "app_secret": "s3cret"}, "baidu": {"api_key": "bk"}},
    )
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.get("/api/config")
        assert resp.status == 200
        body = await resp.json()
        assert body["config"]["feishu"]["app_secret"] == config_server._MASK
        assert body["config"]["baidu"]["api_key"] == config_server._MASK


async def test_put_config_triggers_reload_and_preserves_secret(config_app, tmp_path):
    config_path = config_app[config_server._config_path_key]
    reload_event = config_app[config_server._reload_event_key]
    config_server._write_yaml(
        config_path,
        {"feishu": {"app_id": "cli", "app_secret": "s3cret"}},
    )
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.put(
            "/api/config",
            json={"config": {"feishu": {"app_id": "cli_new", "app_secret": config_server._MASK}}},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

        # reload 事件被触发
        assert reload_event.is_set()

        # 旧密钥被保留
        written = config_server._read_yaml(config_path)
        assert written["feishu"]["app_secret"] == "s3cret"
        assert written["feishu"]["app_id"] == "cli_new"


async def test_put_config_rejects_invalid_body(config_app):
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.put("/api/config", json={"not_config": 1})
        assert resp.status == 422


async def test_get_status_and_health(config_app):
    async with TestClient(TestServer(config_app)) as cli:
        st = await cli.get("/api/status")
        assert st.status == 200
        body = await st.json()
        assert body["running"] is False

        h = await cli.get("/api/health")
        assert h.status == 200
        assert (await h.json())["ok"] is True


# ── 安全校验（Host / Origin 白名单）───────────────────────────────────────


class TestHostAllow:
    def test_accepts_loopback(self):
        assert config_server._host_allowed("127.0.0.1:9191")
        assert config_server._host_allowed("localhost:9191")
        assert config_server._host_allowed("[::1]:9191")

    def test_rejects_foreign(self):
        assert not config_server._host_allowed("evil.com:9191")
        assert not config_server._host_allowed("")
        assert not config_server._host_allowed("127.0.0.1.evil.com")


class TestOriginAllow:
    def test_allows_tauri_and_local(self):
        assert config_server._origin_allowed("tauri://localhost")
        assert config_server._origin_allowed("http://tauri.localhost")
        assert config_server._origin_allowed("null")  # file:// 调试
        assert config_server._origin_allowed("http://localhost:5173")
        assert config_server._origin_allowed("http://127.0.0.1:8080")

    def test_rejects_foreign(self):
        assert not config_server._origin_allowed("https://evil.com")
        assert not config_server._origin_allowed("https://tauri.localhost")  # 必须是 http


async def test_put_config_rejects_foreign_origin(config_app):
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.put(
            "/api/config",
            json={"config": {"feishu": {"app_id": "x"}}},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status == 403
        assert not config_app[config_server._reload_event_key].is_set()


async def test_get_config_allows_local_origin(config_app):
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.get("/api/status", headers={"Origin": "http://localhost:5173"})
        assert resp.status == 200


# ── 连通测试端点 (/api/test-feishu, /api/test-llm) ────────────────────────


def _mocked_aiohttp_session(response_status: int, response_json: dict):
    """构造 aiohttp ClientSession context manager 的 mock。

    返回的对象支持 `async with session.post(...) as resp:` 用法。
    """
    response = MagicMock()
    response.status = response_status
    response.json = AsyncMock(return_value=response_json)

    # response.__aenter__ / __aexit__ 实现 async with
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)

    # session.post(...) 直接返回 response（绕过 await/async with）
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return session


async def test_test_feishu_success(config_app, tmp_path):
    """凭证有效时返回 ok + expire。"""
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path,
        {"feishu": {"app_id": "cli_test", "app_secret": "real_secret"}},
    )
    session = _mocked_aiohttp_session(
        200, {"code": 0, "msg": "ok", "tenant_access_token": "t-xxx", "expire": 7200}
    )
    with patch("xiaopaw.api.config_server.aiohttp.ClientSession", return_value=session):
        async with TestClient(TestServer(config_app)) as cli:
            resp = await cli.post("/api/test-feishu")
            assert resp.status == 200
            body = await resp.json()
    assert body["ok"] is True
    assert body["code"] == 0
    assert body["expire"] == 7200
    assert "latency_ms" in body
    # token 字段不应出现在响应中
    assert "tenant_access_token" not in body


async def test_test_feishu_rejects_missing_credentials(config_app, tmp_path):
    """凭证缺失返回 400。"""
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(config_path, {"feishu": {"app_id": "", "app_secret": ""}})
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.post("/api/test-feishu")
        assert resp.status == 400
        body = await resp.json()
    assert body["ok"] is False
    assert "未配置" in body["error"]


async def test_test_feishu_invalid_credentials(config_app, tmp_path):
    """飞书返回非 0 code 时返回 502 + 错误信息。"""
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path,
        {"feishu": {"app_id": "bad", "app_secret": "wrong"}},
    )
    session = _mocked_aiohttp_session(
        200, {"code": 10003, "msg": "invalid app_secret", "tenant_access_token": ""}
    )
    with patch("xiaopaw.api.config_server.aiohttp.ClientSession", return_value=session):
        async with TestClient(TestServer(config_app)) as cli:
            resp = await cli.post("/api/test-feishu")
            assert resp.status == 502
            body = await resp.json()
    assert body["ok"] is False
    assert body["code"] == 10003
    assert "invalid app_secret" in body["error"]


async def test_test_feishu_network_error(config_app, tmp_path):
    """网络错误返回 502。"""
    import aiohttp

    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path,
        {"feishu": {"app_id": "x", "app_secret": "y"}},
    )

    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientConnectionError("net"))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    with patch("xiaopaw.api.config_server.aiohttp.ClientSession", return_value=session):
        async with TestClient(TestServer(config_app)) as cli:
            resp = await cli.post("/api/test-feishu")
            assert resp.status == 502
            body = await resp.json()
    assert body["ok"] is False
    assert "网络错误" in body["error"]


async def test_test_feishu_timeout(config_app, tmp_path):
    """请求超时返回 502。"""
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path,
        {"feishu": {"app_id": "x", "app_secret": "y"}},
    )

    session = MagicMock()
    session.post = MagicMock(side_effect=asyncio.TimeoutError())
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    with patch("xiaopaw.api.config_server.aiohttp.ClientSession", return_value=session):
        async with TestClient(TestServer(config_app)) as cli:
            resp = await cli.post("/api/test-feishu")
            assert resp.status == 502
            body = await resp.json()
    assert body["ok"] is False
    assert "超时" in body["error"]


async def test_test_llm_success(config_app, tmp_path, monkeypatch):
    """模型连通测试 — 成功路径。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test-xyz")
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path,
        {"agent": {"model": "MiniMax-M3", "api_key": ""}},
    )
    session = _mocked_aiohttp_session(
        200, {"choices": [{"message": {"content": "pong"}}]}
    )
    with patch("xiaopaw.api.config_server.aiohttp.ClientSession", return_value=session):
        async with TestClient(TestServer(config_app)) as cli:
            resp = await cli.post("/api/test-llm")
            assert resp.status == 200
            body = await resp.json()
    assert body["ok"] is True
    assert body["model"] == "MiniMax-M3"
    assert "latency_ms" in body
    monkeypatch.delenv("MINIMAX_API_KEY")


async def test_test_llm_missing_api_key(config_app, tmp_path, monkeypatch):
    """API key 缺失返回 400。"""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(config_path, {"agent": {"model": "MiniMax-M3", "api_key": ""}})
    async with TestClient(TestServer(config_app)) as cli:
        resp = await cli.post("/api/test-llm")
        assert resp.status == 400
        body = await resp.json()
    assert body["ok"] is False


async def test_test_llm_http_error(config_app, tmp_path, monkeypatch):
    """LLM 返回非 200 时返回 502 + 错误信息。"""
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-test")
    config_path = config_app[config_server._config_path_key]
    config_server._write_yaml(
        config_path, {"agent": {"model": "MiniMax-M3", "api_key": ""}}
    )
    session = _mocked_aiohttp_session(
        401, {"error": {"message": "invalid api key"}}
    )
    with patch("xiaopaw.api.config_server.aiohttp.ClientSession", return_value=session):
        async with TestClient(TestServer(config_app)) as cli:
            resp = await cli.post("/api/test-llm")
            assert resp.status == 502
            body = await resp.json()
    assert body["ok"] is False
    assert "invalid api key" in body["error"]
    monkeypatch.delenv("MINIMAX_API_KEY")


# ── 机器人控制端点 (/api/disconnect-feishu, /api/reconnect-feishu) ──────────


@pytest.fixture
def config_app_with_feishu_evt(tmp_path: Path):
    """带 feishu_stop_evt 的 config app fixture。"""
    config_path = tmp_path / "config.yaml"
    reload_event = asyncio.Event()
    feishu_stop_evt = asyncio.Event()
    status: dict = {"running": True, "reloads": 0, "error": None, "feishu_connected": True}
    app = create_config_app(
        config_path, reload_event, status, feishu_stop_evt=feishu_stop_evt
    )
    return app, feishu_stop_evt, reload_event, status


async def test_disconnect_feishu_sets_event(config_app_with_feishu_evt):
    app, feishu_stop_evt, _reload, status = config_app_with_feishu_evt
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/disconnect-feishu")
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert feishu_stop_evt.is_set()
    assert status["feishu_connected"] is False


async def test_disconnect_feishu_without_evt_returns_400(tmp_path):
    """config app 未传 feishu_stop_evt 时调用应返回 400。"""
    config_path = tmp_path / "config.yaml"
    app = create_config_app(
        config_path, asyncio.Event(), {"running": True, "reloads": 0, "error": None}
    )
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/disconnect-feishu")
    assert resp.status == 400


async def test_reconnect_feishu_clears_event_and_triggers_reload(config_app_with_feishu_evt):
    app, feishu_stop_evt, reload_event, status = config_app_with_feishu_evt
    feishu_stop_evt.set()  # 先断开
    reload_event.clear()
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/reconnect-feishu")
        assert resp.status == 200
        body = await resp.json()
    assert body["ok"] is True
    assert not feishu_stop_evt.is_set()
    assert reload_event.is_set()
    assert status["reloads"] == 1


async def test_reconnect_feishu_without_evt_returns_400(tmp_path):
    config_path = tmp_path / "config.yaml"
    app = create_config_app(
        config_path, asyncio.Event(), {"running": True, "reloads": 0, "error": None}
    )
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/reconnect-feishu")
    assert resp.status == 400


# ── /api/status 包含 feishu_connected ──────────────────────────────────────


async def test_status_includes_feishu_connected(config_app_with_feishu_evt):
    app, _evt, _reload, status = config_app_with_feishu_evt
    status["feishu_connected"] = True
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/status")
        body = await resp.json()
    assert body["feishu_connected"] is True

    status["feishu_connected"] = False
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.get("/api/status")
        body = await resp.json()
    assert body["feishu_connected"] is False
