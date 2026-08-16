"""xiaopaw/api/test_server.py 单元测试 — handler HTTP 层（mock Runner + Sender）。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from xiaopaw.api.capture_sender import CaptureSender
from xiaopaw.api.test_server import create_test_app
from xiaopaw.models import InboundMessage


@pytest.fixture
def capture_sender():
    return CaptureSender()


@pytest.fixture
def mock_runner(capture_sender):
    """Mock Runner: dispatch 时通过 capture_sender 立即回复。"""

    async def fake_dispatch(inbound: InboundMessage):
        await capture_sender.send(
            inbound.routing_key,
            f"echo: {inbound.content}",
            inbound.root_id,
        )

    runner = AsyncMock()
    runner.dispatch = AsyncMock(side_effect=fake_dispatch)
    return runner


@pytest.fixture
async def client(mock_runner, capture_sender):
    app = create_test_app(runner=mock_runner, sender=capture_sender)
    async with TestClient(TestServer(app)) as cli:
        yield cli


async def test_message_basic_roundtrip(client):
    """基本消息：发送 → runner dispatch → 通过 CaptureSender 收到回复。"""
    resp = await client.post(
        "/api/test/message",
        json={"routing_key": "p2p:ou_x", "content": "hello"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["reply"] == "echo: hello"
    assert body["session_id"] == ""
    assert body["skills_called"] == []
    assert "msg_id" in body
    assert "duration_ms" in body


async def test_message_invalid_json_returns_400(client):
    """非 JSON body 返回 400。"""
    resp = await client.post(
        "/api/test/message",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status == 400


async def test_message_validation_error_returns_422(client):
    """缺 routing_key 返回 422。"""
    resp = await client.post(
        "/api/test/message",
        json={"content": "no routing_key"},
    )
    assert resp.status == 422


async def test_message_with_explicit_msg_id(client):
    """显式 msg_id 透传。"""
    resp = await client.post(
        "/api/test/message",
        json={
            "routing_key": "p2p:ou_x",
            "content": "x",
            "msg_id": "explicit_id",
            "sender_id": "ou_custom",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["msg_id"] == "explicit_id"


async def test_message_auto_msg_id_when_missing(client):
    """未传 msg_id 时自动生成 test_{uuid}。"""
    resp = await client.post(
        "/api/test/message",
        json={"routing_key": "p2p:ou_x", "content": "auto"},
    )
    body = await resp.json()
    assert body["msg_id"].startswith("test_")


async def test_message_with_attachment_copies_file(client, tmp_path):
    """附件：本地文件复制到 workspace/sessions/{sid}/uploads/。"""
    from xiaopaw.api.test_server import create_test_app as _create

    # 创建 app 带 session_manager + workspace_dir
    import asyncio
    from xiaopaw.session.manager import SessionManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session_mgr = SessionManager(data_dir=tmp_path / "sessions")

    src = tmp_path / "hello.txt"
    src.write_text("hello content", encoding="utf-8")

    cap = CaptureSender()

    async def fake_disp(inbound):
        await cap.send(inbound.routing_key, f"echo: {inbound.content}", inbound.root_id)

    runner = AsyncMock()
    runner.dispatch = AsyncMock(side_effect=fake_disp)

    app = _create(
        runner=runner,
        sender=cap,
        session_mgr=session_mgr,
        workspace_dir=workspace,
    )
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/test/message",
            json={
                "routing_key": "p2p:ou_attach",
                "content": "see file",
                "attachment": {"file_path": str(src), "file_name": "renamed.txt"},
            },
        )
        assert resp.status == 200
        body = await resp.json()
        # 内容应改写为沙盒路径提示
        assert "/workspace/sessions/" in body["reply"]
        assert "renamed.txt" in body["reply"]
        # 文件实际被复制
        copied = workspace / "sessions" / body["session_id"] / "uploads" / "renamed.txt"
        assert copied.exists()
        assert copied.read_text(encoding="utf-8") == "hello content"


async def test_message_attachment_missing_file_returns_hint(client):
    """附件文件不存在时，返回提示但不报错。"""
    import asyncio
    from xiaopaw.session.manager import SessionManager

    cap = CaptureSender()

    async def fake_disp(inbound):
        await cap.send(inbound.routing_key, f"echo: {inbound.content}", inbound.root_id)

    runner = AsyncMock()
    runner.dispatch = AsyncMock(side_effect=fake_disp)

    session_mgr = SessionManager(data_dir=Path("/tmp").parent / "tmp" / "test_no_attach")
    app = create_test_app(runner=runner, sender=cap, session_mgr=session_mgr)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post(
            "/api/test/message",
            json={
                "routing_key": "p2p:ou_x",
                "content": "x",
                "attachment": {"file_path": "/nonexistent/path/file.txt"},
            },
        )
        assert resp.status == 200
        body = await resp.json()
        assert "附件文件不存在" in body["reply"]


async def test_delete_sessions_no_session_mgr(client):
    """未配 SessionManager 时 DELETE 应正常返回。"""
    resp = await client.delete("/api/test/sessions")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"


async def test_delete_sessions_with_session_mgr(client, tmp_path):
    """配 SessionManager 时 DELETE 应清空 sessions。"""
    from xiaopaw.api.test_server import create_test_app as _create
    from xiaopaw.session.manager import SessionManager

    session_mgr = SessionManager(data_dir=tmp_path / "sessions")
    cap = CaptureSender()

    async def fake_disp(inbound):
        await cap.send(inbound.routing_key, "ok", inbound.root_id)

    runner = AsyncMock()
    runner.dispatch = AsyncMock(side_effect=fake_disp)

    app = _create(runner=runner, sender=cap, session_mgr=session_mgr)
    async with TestClient(TestServer(app)) as cli:
        # 先发一条消息创建 session
        r = await cli.post(
            "/api/test/message",
            json={"routing_key": "p2p:ou_x", "content": "hi"},
        )
        assert r.status == 200

        # DELETE 应清空
        resp = await cli.delete("/api/test/sessions")
        assert resp.status == 200
