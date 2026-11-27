"""AnthropicLLM: 실제 API 호출 없이 요청 형태와 거절 처리를 검증한다."""

from types import SimpleNamespace

import pytest

from fivewhys.agent.llm import AnthropicLLM


class StubMessages:
    def __init__(self, stop_reason="tool_use"):
        self.last_kwargs = None
        self.stop_reason = stop_reason

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason=self.stop_reason,
            model=kwargs["model"],
            stop_details=None,
        )


def _client(stop_reason="tool_use"):
    stub = StubMessages(stop_reason)
    return AnthropicLLM(_client=SimpleNamespace(messages=stub)), stub


def test_request_shape():
    llm, stub = _client()
    llm.create(system="sys", messages=[{"role": "user", "content": "hi"}], tools=[])
    kw = stub.last_kwargs
    assert kw["model"] == llm.model
    assert kw["system"] == "sys"
    assert kw["max_tokens"] == llm.max_tokens
    # 최신 모델에서 제거된 샘플링 파라미터를 보내지 않아야 한다
    assert "temperature" not in kw
    assert "top_p" not in kw
    assert "top_k" not in kw


def test_default_model_from_env(monkeypatch):
    monkeypatch.setenv("FIVEWHYS_MODEL", "claude-test-model")
    llm, _ = _client()
    # field default_factory는 인스턴스 생성 시점에 평가된다
    fresh = AnthropicLLM(_client=SimpleNamespace(messages=StubMessages()))
    assert fresh.model == "claude-test-model"


def test_refusal_raises():
    llm, _ = _client(stop_reason="refusal")
    with pytest.raises(RuntimeError, match="거절"):
        llm.create(system="s", messages=[{"role": "user", "content": "x"}], tools=[])
