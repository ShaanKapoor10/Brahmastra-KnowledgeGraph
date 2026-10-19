"""
Which provider serves a request, and what happens when one stops being able to.

The quota case is here because it did real damage: a daily cap landing between
the two calls of one comprehension stored a meeting with its decisions and none
of its risks, on a machine that had a working local model the whole time.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# A spent daily quota, with another provider sitting there working
# ---------------------------------------------------------------------------

def test_a_spent_quota_falls_back_to_a_provider_that_works(monkeypatch):
    """
    LLMQuotaExhausted raises immediately and correctly -- retrying a spent cap
    is as pointless as retrying a retired model. But the caller above it then
    failed, and on this system that failure had a shape: comprehension makes
    two calls per chunk, the cap landed between them, and a meeting was stored
    with four decisions, four action items and ZERO risks. Half a record, from
    a machine with a working local model.
    """
    from brahmastra import llm

    calls: list[str] = []

    def dispatch(name, system, user, **kw):
        calls.append(name)
        if name == "groq":
            raise llm.LLMQuotaExhausted("tokens per day (TPD) exceeded")
        return "{}"

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": True})

    assert llm.chat("s", "u") == "{}"
    assert calls == ["groq", "ollama"], "it did not fall back to the working provider"


def test_a_pinned_provider_is_never_second_guessed(monkeypatch):
    """`provider=` is an explicit choice; silently serving from another one
    would make a pinned comparison meaningless."""
    from brahmastra import llm

    def dispatch(name, system, user, **kw):
        raise llm.LLMQuotaExhausted("tokens per day (TPD) exceeded")

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": True})

    with pytest.raises(llm.LLMQuotaExhausted):
        llm.chat("s", "u", provider="groq")


def test_a_spent_quota_with_nowhere_to_go_still_raises(monkeypatch):
    from brahmastra import llm

    def dispatch(name, system, user, **kw):
        raise llm.LLMQuotaExhausted("tokens per day (TPD) exceeded")

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": False})

    with pytest.raises(llm.LLMQuotaExhausted):
        llm.chat("s", "u")


def test_a_transient_failure_does_not_trigger_a_downgrade(monkeypatch):
    """The retry loop handles those. Falling back on any error would quietly
    move traffic to a smaller model on a blip."""
    from brahmastra import llm

    calls: list[str] = []

    def dispatch(name, system, user, **kw):
        calls.append(name)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(llm, "_dispatch", dispatch)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "groq")
    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "anthropic": False, "ollama": True})

    with pytest.raises(RuntimeError):
        llm.chat("s", "u")
    assert calls == ["groq"]


# -- backing off the way the server asked -----------------------------------
#
# This rule existed and was in the wrong file. extraction.py honoured the delay
# Groq states; `_groq_chat` in llm.py -- the path comprehension, cluster
# summaries, GraphRAG and checkpointing all take -- still slept 2s, 4s, 6s,
# which CLAUDE.md already records as useless: a blind 2+4 covers six seconds of
# a limit the server says needs thirty, so all three attempts land inside the
# same closed window.


def test_the_shared_path_waits_as_long_as_groq_asked():
    from brahmastra.llm import retry_delay

    delay = retry_delay(Exception("Rate limit reached. Please try again in 7.456s"), 0)
    assert delay == pytest.approx(7.556)          # a hair over, not just inside


def test_minutes_in_the_hint_are_read():
    from brahmastra.llm import retry_delay

    assert retry_delay(Exception("try again in 0m32s"), 0) == pytest.approx(32.1)


def test_a_hint_that_would_block_the_run_is_capped():
    """Past the cap, sleeping blocks the caller for work the next run retries
    for free."""
    from brahmastra.llm import retry_delay, max_backoff

    assert retry_delay(Exception("try again in 1m14.2s"), 0) == max_backoff()


def test_a_suspiciously_short_hint_never_beats_the_fallback():
    from brahmastra.llm import retry_delay

    assert retry_delay(Exception("try again in 0.2s"), 0) == 2.0


def test_no_hint_falls_back_to_exponential():
    from brahmastra.llm import retry_delay

    assert retry_delay(Exception("connection reset"), 0) == 2.0
    assert retry_delay(Exception("connection reset"), 1) == 4.0


def test_groq_chat_uses_it_rather_than_a_guess():
    """
    The defect, pinned. A grep for the old constant is the cheapest way to
    stop it coming back, because nothing about `time.sleep(2 * (attempt + 1))`
    looks wrong on its own.
    """
    import inspect
    from brahmastra import groq_pool, llm

    # The wait moved: both Groq call sites go through the key pool, and the
    # pool is where the stated delay is honoured. Pinned in both places so a
    # future edit cannot quietly bring a guessed sleep back into either.
    chat = inspect.getsource(llm._groq_chat)
    assert "groq_pool.pool().call" in chat
    assert "2 * (attempt + 1)" not in chat
    assert "time.sleep" not in chat

    pool = inspect.getsource(groq_pool.GroqKeyPool.call)
    assert "parse_wait(str(exc))" in pool


def test_extraction_shares_the_one_implementation():
    """One rule should not have two implementations that can drift."""
    from brahmastra import extraction, llm

    assert extraction._retry_delay is llm.retry_delay


# -- the provider registry --------------------------------------------------
#
# Groq is what is affordable today, not what this system is for. Its largest
# hosted model is small, and the design has to survive being swapped for
# Gemini or OpenAI without a rewrite. Adding a provider used to mean editing
# six places -- the PROVIDERS tuple, a model default, a model accessor,
# provider_status, active_model and _dispatch -- which is the same shape of
# mistake that already shipped once here: selection was centralised while the
# MODEL stayed hardcoded per call site, so when Groq retired llama-3.3-70b,
# extraction kept calling the dead model while summaries succeeded.


def test_every_provider_declares_everything_needed_to_reach_it():
    """One entry per provider, or the six-places bug comes back."""
    from brahmastra.llm import _REGISTRY

    for spec in _REGISTRY:
        assert spec.name and spec.model_env and spec.default_model, spec
        if spec.is_cloud:
            assert spec.key_env and spec.sdk, f"{spec.name} cannot be reached"


def test_the_registry_is_the_only_list_of_providers():
    from brahmastra.llm import _REGISTRY, PROVIDERS

    assert PROVIDERS == tuple(p.name for p in _REGISTRY)


def test_openai_and_gemini_are_reachable_providers():
    """The two Shaan named as the likely replacements."""
    from brahmastra.llm import PROVIDERS

    assert "openai" in PROVIDERS and "gemini" in PROVIDERS


def test_every_provider_can_be_dispatched_to():
    """
    A provider in the registry that _dispatch does not know is worse than one
    that is absent: it resolves, then fails at call time.
    """
    import inspect
    from brahmastra import llm

    source = inspect.getsource(llm._dispatch)
    for name in llm.PROVIDERS:
        assert f'name == "{name}"' in source, f"_dispatch cannot reach {name}"


def test_every_provider_reports_a_model(monkeypatch):
    from brahmastra.llm import PROVIDERS, model_for

    for name in PROVIDERS:
        assert model_for(name), name


def test_a_model_can_be_overridden_per_provider(monkeypatch):
    from brahmastra.llm import model_for

    monkeypatch.setenv("GEMINI_MODEL", "gemini-3-pro")
    assert model_for("gemini") == "gemini-3-pro"
    monkeypatch.setenv("GEMINI_MODEL", "")          # present-but-empty is unset
    assert model_for("gemini") == "gemini-3.5-flash-lite"


def test_active_model_follows_whichever_provider_is_live(monkeypatch):
    from brahmastra import llm

    monkeypatch.setattr(llm, "resolve_provider", lambda: "gemini")
    assert llm.active_model() == llm.model_for("gemini")
    monkeypatch.setattr(llm, "resolve_provider", lambda: "openai")
    assert llm.active_model() == llm.model_for("openai")


def test_adding_a_key_does_not_silently_change_provider(monkeypatch):
    """
    A key appearing in .env makes a provider AVAILABLE as a fallback; it does
    not move production onto it. A silent switch of the model behind every
    extraction is the kind of change that shows up later as "it got worse"
    with nothing to explain it. LLM_PROVIDER is how you move.
    """
    from brahmastra import llm

    monkeypatch.setattr(llm, "provider_status",
                        lambda: {"groq": True, "openai": True, "gemini": True,
                                 "anthropic": False, "ollama": False})
    monkeypatch.setenv("LLM_PROVIDER", "")
    assert llm.resolve_provider() == "groq"          # unchanged by the new keys

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    assert llm.resolve_provider() == "gemini"        # one line moves it


def test_extraction_reaches_a_new_provider_without_its_own_code(monkeypatch):
    """
    What "not coupled to Groq" has to mean concretely. Groq and Ollama have
    bespoke paths for reasons specific to those vendors -- a 413 that reads
    like a 429, an HTTP endpoint with no SDK -- and a provider without that
    history needs none of it.
    """
    import brahmastra.extraction as extraction
    from brahmastra import llm

    seen = {}

    def fake_chat(system, user, **kwargs):
        seen["provider"] = kwargs.get("provider")
        return '{"triples": []}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(extraction, "resolve_provider", lambda: "gemini",
                        raising=False)
    monkeypatch.setattr(llm, "resolve_provider", lambda: "gemini")

    assert extraction._extract_with_llm("T", "C") == []
    assert seen["provider"] == "gemini"


# ---------------------------------------------------------------------------
# Gemini: thinking is billed, so it is pinned and counted
# ---------------------------------------------------------------------------

def _fake_genai(monkeypatch, usage):
    """A stand-in google.genai that records the config it was called with."""
    import sys
    import types as pytypes

    seen: dict = {}

    class _Config:
        def __init__(self, **kw):
            seen.update(kw)

    class _Thinking:
        def __init__(self, **kw):
            self.kw = kw

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen["model"] = model
            return pytypes.SimpleNamespace(text='{"triples": []}', usage_metadata=usage)

    class _Client:
        def __init__(self, api_key):
            self.models = _Models()

    def _level(value):
        if value not in ("MINIMAL", "LOW", "MEDIUM", "HIGH"):
            raise ValueError(value)
        return value

    gtypes = pytypes.SimpleNamespace(GenerateContentConfig=_Config, ThinkingConfig=_Thinking,
                                     ThinkingLevel=_level)
    genai = pytypes.SimpleNamespace(Client=_Client, types=gtypes)
    google = pytypes.SimpleNamespace(genai=genai)
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", gtypes)
    return seen


def test_gemini_pins_thinking_to_minimal_and_counts_thought_tokens(monkeypatch):
    """
    Gemini bills thinking as output, and 3.x models think by default. Unpinned,
    every extraction pays for reasoning nobody reads -- and LLM_CHARS cannot
    see it, because thoughts are not in the reply text.
    """
    pytest.importorskip("prometheus_client")
    import types as pytypes

    from brahmastra import llm, metrics

    usage = pytypes.SimpleNamespace(prompt_token_count=1200, candidates_token_count=80,
                                    thoughts_token_count=15)
    seen = _fake_genai(monkeypatch, usage)
    monkeypatch.delenv("GEMINI_THINKING_LEVEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    def counted(kind: str) -> float:
        return metrics.LLM_TOKENS.labels("gemini", kind)._value.get()

    before = {k: counted(k) for k in ("input", "output", "thoughts")}
    reply = llm._gemini_chat("system", "user", json_mode=True, temperature=0.0,
                             max_tokens=100, retries=1)

    assert reply == '{"triples": []}'
    assert seen["model"] == "gemini-3.5-flash-lite"
    assert seen["response_mime_type"] == "application/json"
    assert seen["thinking_config"].kw == {"thinking_level": "MINIMAL"}
    assert counted("input") - before["input"] == 1200
    assert counted("output") - before["output"] == 80
    assert counted("thoughts") - before["thoughts"] == 15


def test_gemini_thinking_setting_can_be_left_out(monkeypatch):
    """3.8 Flash has no "minimal" level: "off" must send no setting at all."""
    from brahmastra import llm

    seen = _fake_genai(monkeypatch, None)
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "off")
    llm._gemini_chat("s", "u", json_mode=False, temperature=0.0, max_tokens=10, retries=1)
    assert "thinking_config" not in seen


def test_a_mistyped_thinking_level_fails_with_its_name(monkeypatch):
    from brahmastra import llm

    _fake_genai(monkeypatch, None)
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "minimum")
    with pytest.raises(llm.LLMUnavailable, match="GEMINI_THINKING_LEVEL"):
        llm._gemini_chat("s", "u", json_mode=False, temperature=0.0, max_tokens=10, retries=1)
