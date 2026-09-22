"""tts_minimax + audio_helpers.synthesize_tts 单元测试 (mock HTTP)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.utils.tts_minimax import (
    synthesize_minimax,
    MiniMaxTTSError,
    DEFAULT_VOICE_ID,
)
from src.utils import audio_helpers


class _FakeResp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


def _ok_post_fn(audio_hex: str):
    def _post(url, payload, headers, timeout):
        return _FakeResp(200, {
            "data": {"audio": audio_hex},
            "base_resp": {"status_code": 0, "status_msg": "success"},
        })
    return _post


# ----- synthesize_minimax 直测 -----

def test_synthesize_minimax_ok():
    fake_mp3 = b"ID3\x03\x00\x00\x00fakebytes"
    out = synthesize_minimax(
        "你好", api_key="sk-test",
        _post_fn=_ok_post_fn(fake_mp3.hex()),
    )
    assert out == fake_mp3


def test_synthesize_minimax_http_500():
    def _post(*a, **kw):
        return _FakeResp(500, text="internal error")
    with pytest.raises(MiniMaxTTSError, match="HTTP 500"):
        synthesize_minimax("你好", api_key="sk-test", retries=0, _post_fn=_post)


def test_synthesize_minimax_business_error():
    def _post(*a, **kw):
        return _FakeResp(200, {"data": {}, "base_resp": {"status_code": 1004, "status_msg": "rate limit"}})
    with pytest.raises(MiniMaxTTSError, match="status=1004"):
        synthesize_minimax("你好", api_key="sk-test", retries=0, _post_fn=_post)


def test_synthesize_minimax_missing_audio():
    def _post(*a, **kw):
        return _FakeResp(200, {"data": {}, "base_resp": {"status_code": 0}})
    with pytest.raises(MiniMaxTTSError, match="data.audio"):
        synthesize_minimax("你好", api_key="sk-test", retries=0, _post_fn=_post)


def test_synthesize_minimax_empty_text_raises():
    with pytest.raises(MiniMaxTTSError, match="text 为空"):
        synthesize_minimax("  ", api_key="sk-test")


def test_synthesize_minimax_no_apikey_raises():
    with pytest.raises(MiniMaxTTSError, match="api_key 缺失"):
        synthesize_minimax("hi", api_key="")


def test_synthesize_minimax_payload_shape():
    captured = {}
    def _post(url, payload, headers, timeout):
        captured["url"] = url
        captured["payload"] = payload
        captured["headers"] = headers
        return _FakeResp(200, {"data": {"audio": b"\x00\x01".hex()}, "base_resp": {"status_code": 0}})
    synthesize_minimax(
        "测试", api_key="sk-x", model="speech-2.8-hd", voice_id="vx",
        speed=1.2, _post_fn=_post,
    )
    assert captured["url"].endswith("/t2a_v2")
    assert captured["payload"]["model"] == "speech-2.8-hd"
    assert captured["payload"]["text"] == "测试"
    assert captured["payload"]["voice_setting"]["voice_id"] == "vx"
    assert captured["payload"]["voice_setting"]["speed"] == 1.2
    assert captured["payload"]["audio_setting"]["format"] == "mp3"
    assert captured["payload"]["stream"] is False
    assert captured["headers"]["Authorization"] == "Bearer sk-x"


# ----- audio_helpers 路由 -----

def test_is_minimax_model():
    assert audio_helpers._is_minimax_model("speech-2.8-hd")
    assert audio_helpers._is_minimax_model("Speech-02-HD")
    assert audio_helpers._is_minimax_model("minimax-tts")
    assert not audio_helpers._is_minimax_model("cosyvoice-v2")
    assert not audio_helpers._is_minimax_model("longxiaochun_v2")
    assert not audio_helpers._is_minimax_model("")


def test_synthesize_tts_routes_minimax(monkeypatch):
    monkeypatch.setattr(audio_helpers, "get_tts_config",
                        lambda: ("speech-2.8-hd", "male-qn-qingse"))
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_key", lambda: "sk-x")
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_voice", lambda: "vx")
    fake_mp3 = b"\xff\xfb\x90fake-mp3"
    called = {}
    def fake_synth(text, *, api_key, model, voice_id):
        called["model"] = model
        called["voice_id"] = voice_id
        called["api_key"] = api_key
        return fake_mp3
    import src.utils.tts_minimax as mm
    monkeypatch.setattr(mm, "synthesize_minimax", fake_synth)
    out = audio_helpers.synthesize_tts("你好")
    assert out == fake_mp3
    assert called == {"model": "speech-2.8-hd", "voice_id": "vx", "api_key": "sk-x"}


def _forbid_dashscope(monkeypatch):
    """MiniMax 的兜底已经从 dashscope 换成 Qwen-TTS, dashscope 一旦被调就是回归。"""
    def _boom(*args, **kwargs):
        raise AssertionError("MiniMax 兜底不应再走 dashscope(会中途变声)")
    monkeypatch.setattr(audio_helpers, "_synthesize_dashscope", _boom)


def test_synthesize_tts_minimax_no_key_falls_back_qwen(monkeypatch):
    """缺 minimax key → 回退 Qwen-TTS 男声(QWEN_TTS_DEFAULT_VOICE), 不走 dashscope。"""
    monkeypatch.setattr(audio_helpers, "get_tts_config",
                        lambda: ("speech-2.8-hd", "male-qn-qingse"))
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_key", lambda: None)
    _forbid_dashscope(monkeypatch)
    captured = {}

    def fake_qwen(text, model=None, voice=None):
        captured["text"] = text
        captured["voice"] = voice
        return b"QWEN_OK"

    monkeypatch.setattr(audio_helpers, "_synthesize_qwen", fake_qwen)
    out = audio_helpers.synthesize_tts("hi")
    assert out == b"QWEN_OK"
    assert captured["text"] == "hi"
    assert captured["voice"] == audio_helpers.QWEN_TTS_DEFAULT_VOICE


def test_synthesize_tts_minimax_failure_falls_back_qwen(monkeypatch):
    """MiniMax 调用失败 → 同样回退 Qwen-TTS 男声。"""
    monkeypatch.setattr(audio_helpers, "get_tts_config",
                        lambda: ("speech-2.8-hd", "vx"))
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_key", lambda: "sk-x")
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_voice", lambda: "vx")
    _forbid_dashscope(monkeypatch)
    import src.utils.tts_minimax as mm

    def fake_synth_fail(text, *, api_key, model, voice_id):
        raise mm.MiniMaxTTSError("network down")

    monkeypatch.setattr(mm, "synthesize_minimax", fake_synth_fail)
    captured = {}

    def fake_qwen(text, model=None, voice=None):
        captured["voice"] = voice
        return b"QWEN_FALLBACK"

    monkeypatch.setattr(audio_helpers, "_synthesize_qwen", fake_qwen)
    out = audio_helpers.synthesize_tts("hi")
    assert out == b"QWEN_FALLBACK"
    assert captured["voice"] == audio_helpers.QWEN_TTS_DEFAULT_VOICE


def test_synthesize_tts_default_routes_dashscope(monkeypatch):
    monkeypatch.setattr(audio_helpers, "get_tts_config",
                        lambda: ("cosyvoice-v2", "longxiaochun_v2"))
    captured = {}
    def fake_dashscope(text, model, voice):
        captured["model"] = model
        captured["voice"] = voice
        return b"DS_OK"
    monkeypatch.setattr(audio_helpers, "_synthesize_dashscope", fake_dashscope)
    out = audio_helpers.synthesize_tts("hi")
    assert out == b"DS_OK"
    assert captured == {"model": "cosyvoice-v2", "voice": "longxiaochun_v2"}


def test_make_tts_synthesizer_returns_minimax_adapter(monkeypatch):
    monkeypatch.setattr(audio_helpers, "get_tts_config",
                        lambda: ("speech-2.8-hd", "vx"))
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_key", lambda: "sk-x")
    monkeypatch.setattr(audio_helpers, "_resolve_minimax_voice", lambda: "vx")
    ss = audio_helpers.make_tts_synthesizer()
    assert isinstance(ss, audio_helpers._MiniMaxSynthesizerAdapter)
    assert hasattr(ss, "call")
