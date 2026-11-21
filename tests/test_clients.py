"""Wiring tests for the real Token Factory / Tavily wrappers, with the network mocked out."""
from types import SimpleNamespace

import httpx
import openai
import pytest

from priorart.cache import Cache
from priorart.config import Settings
from priorart.llm import LLM, LLMError
from priorart.search import Search


def _reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def _api_error(cls, status, msg):
    resp = httpx.Response(status, request=httpx.Request("POST", "https://api.tokenfactory.nebius.com/v1/chat/completions"))
    return cls(msg, response=resp, body=None)


class _Completions:
    def __init__(self, reject_json_mode=False, fail=None, fail_times=0):
        self.calls = []
        self.reject = reject_json_mode
        self.fail, self.fail_times = fail, fail_times

    def create(self, **kw):
        self.calls.append(kw)
        if self.fail is not None and self.fail_times:
            self.fail_times -= 1
            raise self.fail
        if self.reject and "response_format" in kw:
            raise _api_error(openai.BadRequestError, 400, "response_format is not supported for this model")
        return _reply('<think>x</think>{"ok": true}')


def _llm(tmp_path, reject=False, **kw):
    s = Settings(nebius_api_key="k", tavily_api_key="t")
    llm = LLM(s, Cache(str(tmp_path / "c.sqlite")))
    comp = _Completions(reject, **kw)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return llm, comp


def test_llm_json_mode_and_cache(tmp_path):
    llm, comp = _llm(tmp_path)
    assert llm.json("t", "sys", {"a": 1}) == {"ok": True}
    assert comp.calls[0]["response_format"] == {"type": "json_object"}
    assert comp.calls[0]["model"] == llm.s.primary_model
    assert llm.json("t", "sys", {"a": 1}) == {"ok": True}
    assert len(comp.calls) == 1  # second call served from cache
    llm.json("t", "sys", {"a": 1}, role="verifier")
    assert comp.calls[-1]["model"] == llm.s.verifier_model


def test_llm_falls_back_without_json_mode(tmp_path):
    llm, comp = _llm(tmp_path, reject=True)
    assert llm.json("t", "sys", {"b": 2}) == {"ok": True}
    assert "response_format" not in comp.calls[-1]


def test_wrong_model_fails_fast_with_fix(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: (_ for _ in ()).throw(AssertionError("should not retry")))
    llm, comp = _llm(tmp_path, fail=_api_error(openai.NotFoundError, 404, "model not found"), fail_times=9)
    with pytest.raises(LLMError, match="agent.py models"):
        llm.json("t", "sys", {"c": 3})
    assert len(comp.calls) == 1


def test_bad_key_fails_fast(tmp_path):
    llm, _ = _llm(tmp_path, fail=_api_error(openai.AuthenticationError, 401, "bad key"), fail_times=9)
    with pytest.raises(LLMError, match="NEBIUS_API_KEY"):
        llm.json("t", "sys", {"d": 4})


def test_transient_errors_are_retried(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm, comp = _llm(tmp_path, fail=_api_error(openai.RateLimitError, 429, "slow down"), fail_times=2)
    assert llm.json("t", "sys", {"e": 5}) == {"ok": True}
    assert len(comp.calls) == 3


def test_search_wrapper(tmp_path):
    s = Settings(nebius_api_key="k", tavily_api_key="t")
    srch = Search(s, Cache(str(tmp_path / "c.sqlite")))
    seen = {}

    class FakeTavily:
        def search(self, **kw):
            seen["search"] = kw
            return {"results": [{"title": "X", "url": "https://github.com/a/x", "content": "c" * 900, "score": 0.8}]}

        def extract(self, **kw):
            seen.setdefault("extract", []).append(kw)
            return {"results": [{"url": u, "raw_content": "page " + u} for u in kw["urls"]], "failed_results": []}

    srch.client = FakeTavily()
    r = srch.search("github", "q")
    assert seen["search"]["include_domains"] == ["github.com"]
    assert len(r[0]["snippet"]) == 600
    srch.search("web", "q2")
    assert "include_domains" not in seen["search"]
    pages = srch.extract([f"https://e.com/{i}" for i in range(25)])
    assert len(pages) == 25 and len(seen["extract"]) == 2  # batched in 20s
