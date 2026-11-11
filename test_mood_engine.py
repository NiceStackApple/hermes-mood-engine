"""Self-check tests for mood-engine v1.1 plugin."""

from __future__ import annotations

import contextvars
import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace

_PLUGIN_PATH = Path(__file__).with_name("__init__.py")
_SPEC = importlib.util.spec_from_file_location("mood_engine", _PLUGIN_PATH)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


class _FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class _FakeLlm:
    def __init__(self, parsed=None, fail=False):
        self.parsed = parsed
        self.fail = fail
        self.calls = 0

    def complete_structured(self, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        return SimpleNamespace(parsed=self.parsed)


class _FakeCtx:
    def __init__(self, evaluator_mode="heuristic", llm=None):
        self.state = _FakeState()
        self.hooks = {}
        self.commands = {}
        self.config = {"evaluator_mode": evaluator_mode}
        self.llm = llm or _FakeLlm(fail=True)

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, **kwargs):
        self.commands[name] = fn


def run_tests() -> None:
    # 1. Neutral baseline returns None (zero prompt token overhead)
    ctx = _FakeCtx(evaluator_mode="heuristic")
    mod.register(ctx)
    assert ctx.hooks["pre_llm_call"](user_message="hai mei") is None

    # 2. Greeting "hai" is NOT treated as dry text, and "gitu" does NOT match "git"
    ctx.hooks["post_llm_call"](user_message="hai mei, lho kok gitu")
    s0 = ctx.state.get("mood")
    assert s0["patience"] == 50.0 and s0["focus"] == 50.0

    # 3. Patience thresholds: <45 -> sulking, <20 -> heavy dry text
    d44 = mod.build_directives({"energy": 50, "warmth": 50, "patience": 44, "focus": 50})
    assert any("patience < 45" in d for d in d44)
    d19 = mod.build_directives({"energy": 50, "warmth": 50, "patience": 19, "focus": 50})
    assert any("patience < 20" in d and "dry text" in d for d in d19)

    # 4. Passive decay toward 50 after 2 consecutive neutral turns
    s = mod._default_state()
    s["energy"] = 80.0
    s1 = mod.apply_turn_deltas(s, {"e_delta": 0, "w_delta": 0, "p_delta": 0, "f_delta": 0}, step_size=12)
    assert s1["energy"] == 80.0
    s2 = mod.apply_turn_deltas(s1, {"e_delta": 0, "w_delta": 0, "p_delta": 0, "f_delta": 0}, step_size=12)
    assert s2["energy"] == 75.0

    # 5. Async auto mode uses ctx.llm when available, and falls back to heuristic when offline
    llm_ok = _FakeLlm(parsed={"e_delta": 1, "w_delta": 1, "p_delta": 0, "f_delta": 1, "reason": "excited tech chat"})
    ctx_llm = _FakeCtx(evaluator_mode="auto", llm=llm_ok)
    mod.register(ctx_llm)
    ctx_llm.hooks["post_llm_call"](user_message="gas bikin plugin", assistant_response="ayo!")
    deadline = time.time() + 2.0
    while ctx_llm.state.get("mood") is None and time.time() < deadline:
        time.sleep(0.02)
    s_llm = ctx_llm.state.get("mood")
    assert s_llm is not None and s_llm["last_evaluator"] == "llm" and s_llm["focus"] == 62.0

    ctx_fb = _FakeCtx(evaluator_mode="auto", llm=_FakeLlm(fail=True))
    mod.register(ctx_fb)
    ctx_fb.hooks["post_llm_call"](user_message="Y", assistant_response="oke")
    deadline = time.time() + 2.0
    while ctx_fb.state.get("mood") is None and time.time() < deadline:
        time.sleep(0.02)
    s_fb = ctx_fb.state.get("mood")
    assert s_fb is not None and s_fb["last_evaluator"] == "heuristic" and s_fb["patience"] < 50.0

    # ContextVars propagation across background thread (multiplexed profile isolation)
    profile_var: contextvars.ContextVar[str] = contextvars.ContextVar("profile_var", default="default")
    seen_profiles: list[str] = []

    class _ScopedLlm(_FakeLlm):
        def complete_structured(self, **kwargs):
            seen_profiles.append(profile_var.get())
            return super().complete_structured(**kwargs)

    ctx_scoped = _FakeCtx(evaluator_mode="auto", llm=_ScopedLlm(
        parsed={"e_delta": 0, "w_delta": 1, "p_delta": 0, "f_delta": 0, "reason": "scoped ok"}
    ))
    mod.register(ctx_scoped)
    tok = profile_var.set("meina")
    try:
        ctx_scoped.hooks["post_llm_call"](user_message="makasih mei", assistant_response="sama-sama")
    finally:
        profile_var.reset(tok)
    deadline = time.time() + 2.0
    while ctx_scoped.state.get("mood") is None and time.time() < deadline:
        time.sleep(0.02)
    assert seen_profiles == ["meina"], f"expected ['meina'], got {seen_profiles}"

    # 6. Idle time drift & internal prefixes & slash commands
    now = time.time()
    drifted = mod.apply_time_drift(
        {"energy": 20.0, "warmth": 20.0, "patience": 15.0, "focus": 50.0, "turns": 3, "updated_at": now - 7200},
        now=now,
        local_hour=12,
    )
    assert drifted["patience"] > 15.0 and drifted["energy"] > 20.0
    assert ctx.hooks["pre_llm_call"](user_message="[SYSTEM TIME NOTICE: x]") is None
    assert "badmood" in ctx.commands["mood"]("set badmood")
    assert "Mood: badmood" in ctx.commands["mood"]("why")
    assert "reset" in ctx.commands["mood"]("reset").lower()

    print("mood-engine v1.1 self-check passed (6/6 assertions)")


if __name__ == "__main__":
    run_tests()
