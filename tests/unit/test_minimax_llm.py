"""MiniMaxLLM 单元测试 — 最小化冒烟测试（不调用真实 API）。"""

from __future__ import annotations

import pytest

from xiaopaw.llm.minimax_llm import MiniMaxLLM


def _make_llm(**kwargs) -> MiniMaxLLM:
    defaults = {"model": "MiniMax-M3", "api_key": "test-key"}
    return MiniMaxLLM(**{**defaults, **kwargs})


class TestMiniMaxLLMInit:
    def test_minimal_init(self):
        llm = _make_llm()
        assert llm.model == "MiniMax-M3"
        assert llm.api_key == "test-key"
        assert llm.endpoint == "https://api.minimaxi.com/v1/chat/completions"
        assert llm.temperature is None
        assert llm.image_model == "MiniMax-VL-01"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
        llm = MiniMaxLLM(model="MiniMax-M3")
        assert llm.api_key == "env-key"

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        with pytest.raises(ValueError, match="MINIMAX_API_KEY"):
            MiniMaxLLM(model="MiniMax-M3")

    def test_custom_base_url(self):
        llm = _make_llm(base_url="https://example.com/v1/")
        assert llm.endpoint == "https://example.com/v1/chat/completions"

    def test_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_BASE_URL", "https://env.example.com/v1")
        llm = _make_llm()
        assert llm.base_url == "https://env.example.com/v1"

    def test_custom_image_model(self):
        llm = _make_llm(image_model="custom-vl")
        assert llm.image_model == "custom-vl"

    def test_image_model_from_env(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_IMAGE_MODEL", "env-vl")
        llm = _make_llm()
        assert llm.image_model == "env-vl"

    def test_temperature_passed_through(self):
        llm = _make_llm(temperature=0.7)
        assert llm.temperature == 0.7


class TestMiniMaxLLMCapabilities:
    def test_context_window_m3(self):
        assert _make_llm(model="MiniMax-M3").get_context_window_size() == 1_048_576

    def test_context_window_m2(self):
        assert _make_llm(model="MiniMax-M2").get_context_window_size() == 1_048_576

    def test_context_window_01(self):
        assert _make_llm(model="MiniMax-01").get_context_window_size() == 1_048_576

    def test_context_window_vl(self):
        assert _make_llm(model="MiniMax-VL-01").get_context_window_size() == 16_384

    def test_context_window_default(self):
        assert _make_llm(model="MiniMax-Text-01").get_context_window_size() == 32_768

    def test_supports_function_calling(self):
        assert _make_llm().supports_function_calling() is True

    def test_supports_stop_words(self):
        assert _make_llm().supports_stop_words() is True


class TestMiniMaxLLMHelpers:
    def test_prepare_stop_words_str(self):
        llm = _make_llm()
        assert llm._prepare_stop_words("END") == "END"

    def test_prepare_stop_words_list(self):
        llm = _make_llm()
        assert llm._prepare_stop_words(["END", "STOP"]) == ["END", "STOP"]

    def test_prepare_stop_words_empty(self):
        llm = _make_llm()
        assert llm._prepare_stop_words("") is None
        assert llm._prepare_stop_words([]) is None
        assert llm._prepare_stop_words(None) is None
