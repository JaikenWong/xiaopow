"""xiaopaw/api/schemas.py 单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from xiaopaw.api.schemas import TestAttachment, TestRequest, TestResponse


class TestTestAttachment:
    def test_minimal(self):
        a = TestAttachment(file_path="/tmp/foo.txt")
        assert a.file_path == "/tmp/foo.txt"
        assert a.file_name is None

    def test_with_filename(self):
        a = TestAttachment(file_path="/tmp/x", file_name="custom.txt")
        assert a.file_name == "custom.txt"

    def test_missing_file_path_raises(self):
        with pytest.raises(ValidationError):
            TestAttachment(file_name="x")  # type: ignore[call-arg]


class TestTestRequest:
    def test_minimal_only_routing_key(self):
        r = TestRequest(routing_key="p2p:ou_x")
        assert r.routing_key == "p2p:ou_x"
        assert r.content == ""
        assert r.msg_id is None
        assert r.sender_id == "ou_test001"
        assert r.attachment is None

    def test_full_request(self):
        r = TestRequest(
            routing_key="group:oc_x",
            content="hi",
            msg_id="custom_id",
            sender_id="ou_custom",
            attachment=TestAttachment(file_path="/tmp/x", file_name="x.txt"),
        )
        assert r.content == "hi"
        assert r.msg_id == "custom_id"
        assert r.sender_id == "ou_custom"
        assert r.attachment is not None
        assert r.attachment.file_name == "x.txt"

    def test_missing_routing_key_raises(self):
        with pytest.raises(ValidationError):
            TestRequest(content="hi")  # type: ignore[call-arg]


class TestTestResponse:
    def test_minimal(self):
        r = TestResponse(
            msg_id="m1",
            reply="pong",
            session_id="s1",
            duration_ms=100,
        )
        assert r.msg_id == "m1"
        assert r.reply == "pong"
        assert r.skills_called == []

    def test_with_skills(self):
        r = TestResponse(
            msg_id="m2",
            reply="done",
            session_id="s2",
            duration_ms=200,
            skills_called=["pdf", "docx"],
        )
        assert r.skills_called == ["pdf", "docx"]

    def test_missing_required_raises(self):
        with pytest.raises(ValidationError):
            TestResponse(msg_id="x")  # type: ignore[call-arg]

    def test_dump_roundtrip(self):
        r = TestResponse(
            msg_id="m3",
            reply="r",
            session_id="s3",
            duration_ms=10,
            skills_called=["x"],
        )
        data = r.model_dump()
        assert data["msg_id"] == "m3"
        assert data["skills_called"] == ["x"]
        # 反向重建
        r2 = TestResponse(**data)
        assert r2 == r
