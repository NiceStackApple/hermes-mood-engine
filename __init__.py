"""Dynamic 4-axis mood engine plugin for Hermes Agent."""

from __future__ import annotations

import contextvars
from datetime import datetime
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_AXES = ("energy", "warmth", "patience", "focus")
_NEUTRAL = 50.0

_PRESETS: Dict[str, Dict[str, float]] = {
    "neutral": {"energy": 50.0, "warmth": 50.0, "patience": 50.0, "focus": 50.0},
    "playful": {"energy": 80.0, "warmth": 80.0, "patience": 75.0, "focus": 40.0},
    "warm": {"energy": 65.0, "warmth": 85.0, "patience": 80.0, "focus": 50.0},
    "focused": {"energy": 68.0, "warmth": 55.0, "patience": 65.0, "focus": 88.0},
    "tired": {"energy": 20.0, "warmth": 55.0, "patience": 50.0, "focus": 35.0},
    "annoyed": {"energy": 55.0, "warmth": 32.0, "patience": 36.0, "focus": 50.0},
    "badmood": {"energy": 40.0, "warmth": 22.0, "patience": 15.0, "focus": 35.0},
}

_INTERNAL_PREFIXES = (
    "[IMPORTANT:",
    "[INTERNAL NOTIFICATION",
    "[OUT-OF-BAND USER MESSAGE",
    "[SYSTEM TIME NOTICE",
    "[System Time Notice",
    "[ASYNC DELEGATION COMPLETE",
    "Cronjob Response:",
)

_EVALUATOR_SCHEMA = {
    "type": "object",
    "properties": {
        "e_delta": {"type": "integer", "enum": [-1, 0, 1]},
        "w_delta": {"type": "integer", "enum": [-1, 0, 1]},
        "p_delta": {"type": "integer", "enum": [-1, 0, 1]},
        "f_delta": {"type": "integer", "enum": [-1, 0, 1]},
        "reason": {"type": "string"},
    },
    "required": ["e_delta", "w_delta", "p_delta", "f_delta", "reason"],
    "additionalProperties": False,
}


def _compile_terms(terms: tuple[str, ...]) -> re.Pattern[str]:
    escaped = "|".join(re.escape(t) for t in terms)
    return re.compile(rf"\b(?:{escaped})\b", re.IGNORECASE)


_WARM_RE = _compile_terms((
    "makasih", "thanks", "thank you", "hehe", "haha", "wkwk", "ciee",
    "keren", "mantap", "asik", "seru", "semangat", "sayang", "lucu", "good job",
))
_SOOTHE_RE = _compile_terms((
    "maaf", "sorry", "sori", "sory", "jangan marah", "jangan ngambek",
    "canda", "bercanda", "hibur", "jajan", "eskrim", "es krim", "kopi",
    "beneran", "yaudah maaf", "ututu", "cup cup",
))
_HARSH_RE = _compile_terms((
    "bodoh", "tolol", "goblok", "bego", "sampah", "useless", "stupid",
    "lelet", "lama banget", "kesel", "males", "bacot", "shut up", "sok tau",
))
_TECH_RE = _compile_terms((
    "bug", "error", "code", "plugin", "hook", "api", "python", "git",
    "deploy", "deadline", "hackathon", "uts", "tugas", "script", "debug", "test",
))
_BANTER_RE = _compile_terms((
    "gabut", "mabar", "ngopi", "jajan", "gombal", "jokes", "becanda", "bercanda",
))
_TIRED_RE = _compile_terms((
    "capek", "ngantuk", "lelah", "tired", "sleepy", "begadang", "pusing",
))

_DRY_WORDS = {
    "y", "ok", "oke", "g", "ya", "hm", "hmm", "hmmm", "terserah",
    "k", "oh", "o", "gt", "iya", "serah", "bodo",
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, round(value, 1)))


def _clamp_delta(val: Any) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return 0
    return -1 if n < 0 else (1 if n > 0 else 0)


def _default_state(now: Optional[float] = None) -> Dict[str, Any]:
    ts = time.time() if now is None else now
    return {
        **{axis: _NEUTRAL for axis in _AXES},
        "idle_turns": {axis: 0 for axis in _AXES},
        "turns": 0,
        "last_reason": "neutral baseline",
        "last_evaluator": "init",
        "updated_at": ts,
    }


def _normalize_state(raw: Any, now: Optional[float] = None) -> Dict[str, Any]:
    base = _default_state(now)
    if not isinstance(raw, dict):
        return base
    for axis in _AXES:
        try:
            base[axis] = _clamp(float(raw.get(axis, _NEUTRAL)))
        except (TypeError, ValueError):
            pass
    raw_idle = raw.get("idle_turns")
    if isinstance(raw_idle, dict):
        for axis in _AXES:
            try:
                base["idle_turns"][axis] = max(0, int(raw_idle.get(axis, 0)))
            except (TypeError, ValueError):
                pass
    try:
        base["turns"] = max(0, int(raw.get("turns", 0)))
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("last_reason"), str) and raw["last_reason"].strip():
        base["last_reason"] = raw["last_reason"].strip()[:120]
    if isinstance(raw.get("last_evaluator"), str) and raw["last_evaluator"].strip():
        base["last_evaluator"] = raw["last_evaluator"].strip()[:24]
    try:
        base["updated_at"] = float(raw.get("updated_at", base["updated_at"]))
    except (TypeError, ValueError):
        pass
    return base


def apply_time_drift(
    state: Dict[str, Any],
    *,
    now: Optional[float] = None,
    local_hour: Optional[int] = None,
    recovery_per_hour: float = 8.0,
    night_fatigue: bool = True,
) -> Dict[str, Any]:
    """Drift axes gently toward neutral (50) over idle hours."""
    cur = _normalize_state(state, now=now)
    ts = time.time() if now is None else now
    elapsed_hours = max(0.0, (ts - float(cur.get("updated_at", ts))) / 3600.0)
    if elapsed_hours > 0.05:
        step = min(1.0, (elapsed_hours * max(0.0, recovery_per_hour)) / 40.0)
        for axis in _AXES:
            cur[axis] = _clamp(float(cur[axis]) + (_NEUTRAL - float(cur[axis])) * step)

    hour = datetime.now().astimezone().hour if local_hour is None else local_hour
    if night_fatigue and (hour >= 23 or hour < 5):
        cur["energy"] = _clamp(min(float(cur["energy"]), 48.0))
    return cur


def classify_mood(state: Dict[str, Any]) -> str:
    energy = float(state["energy"])
    warmth = float(state["warmth"])
    patience = float(state["patience"])
    focus = float(state["focus"])

    if patience < 20:
        return "badmood"
    if patience < 45 or warmth <= 35:
        return "annoyed"
    if energy <= 35:
        return "tired"
    if focus >= 65 and energy > 35:
        return "focused"
    if warmth >= 65 and energy >= 65:
        return "playful"
    if warmth >= 65:
        return "warm"
    return "neutral"


def build_directives(state: Dict[str, Any]) -> List[str]:
    """Return active per-axis style directives; empty when all axes are neutral."""
    rules: List[str] = []
    energy = float(state["energy"])
    warmth = float(state["warmth"])
    patience = float(state["patience"])
    focus = float(state["focus"])

    if energy <= 35:
        rules.append("low energy: lowercase all, abbreviated casual phrasing, minimal punctuation, act tired/passive")
    elif energy >= 65:
        rules.append("high energy: enthusiastic, lively, expressive punctuation, ask follow-ups")

    if warmth <= 35:
        rules.append("low warmth: distant, cold, zero laughter (no wkwk/haha), matter-of-fact")
    elif warmth >= 65:
        rules.append("high warmth: very warm, attentive, close companion tone with light teasing")

    if patience < 20:
        rules.append(
            "heavy sulk (patience < 20): reply mostly with very short dry text "
            "(e.g. 'Y', 'G', 'hm', 'terserah'), reluctant to help until cheered up or apologized to"
        )
    elif patience < 45:
        rules.append(
            "sulking (patience < 45): short, dry/snappy replies, end final sentence with a period (.), "
            "slight sarcasm, do not be overly accommodating"
        )
    elif patience >= 65:
        rules.append("high patience: calm, soothing, understanding even with repeated questions")

    if focus <= 35:
        rules.append("low focus: easily sidetracked, lean into jokes or banter over serious tasks")
    elif focus >= 65:
        rules.append("high focus: cut small talk, go straight to technical substance and concrete solutions")

    return rules


def format_mood_context(state: Dict[str, Any], *, inject_in_neutral: bool = False) -> Optional[str]:
    rules = build_directives(state)
    if not rules and not inject_in_neutral:
        return None
    mood = classify_mood(state)
    e = int(round(float(state["energy"])))
    w = int(round(float(state["warmth"])))
    p = int(round(float(state["patience"])))
    f = int(round(float(state["focus"])))
    if not rules:
        return f"[Mood Engine: mood={mood} | E={e} W={w} P={p} F={f}]"
    return f"[Mood Engine: mood={mood} | E={e} W={w} P={p} F={f} | directives: {'; '.join(rules)}]"


def evaluate_heuristic_deltas(user_message: str) -> Dict[str, Any]:
    """Deterministic fallback evaluator when LLM background evaluation is off or fails."""
    text = " ".join(user_message.strip().split())
    lower = text.lower()
    e_d = w_d = p_d = f_d = 0
    reasons: List[str] = []

    if _WARM_RE.search(text):
        w_d += 1
        p_d += 1
        e_d += 1
        reasons.append("warm/friendly")

    if _SOOTHE_RE.search(text):
        w_d += 1
        p_d += 1
        reasons.append("soothing/apology")

    if _HARSH_RE.search(text):
        w_d -= 1
        p_d -= 1
        reasons.append("harsh/frustrated")

    if lower in _DRY_WORDS or (len(lower) == 1 and lower.isalpha()):
        w_d -= 1
        p_d -= 1
        e_d -= 1
        reasons.append("dry/one-word reply")

    if _TECH_RE.search(text):
        f_d += 1
        reasons.append("technical/task focus")
    elif _BANTER_RE.search(text):
        f_d -= 1
        reasons.append("casual banter")

    if _TIRED_RE.search(text):
        e_d -= 1
        reasons.append("low energy")

    if "!" in text and len(text) > 12 and not _HARSH_RE.search(text):
        e_d += 1

    return {
        "e_delta": _clamp_delta(e_d),
        "w_delta": _clamp_delta(w_d),
        "p_delta": _clamp_delta(p_d),
        "f_delta": _clamp_delta(f_d),
        "reason": ", ".join(reasons) if reasons else "neutral exchange",
    }


def apply_turn_deltas(
    state: Dict[str, Any],
    deltas: Dict[str, Any],
    *,
    step_size: float = 12.0,
    evaluator: str = "heuristic",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Apply (-1/0/+1) deltas with momentum and 2-turn passive decay toward 50."""
    cur = _normalize_state(state, now=now)
    step = max(1.0, min(30.0, float(step_size)))
    delta_keys = (
        ("energy", "e_delta"),
        ("warmth", "w_delta"),
        ("patience", "p_delta"),
        ("focus", "f_delta"),
    )
    for axis, key in delta_keys:
        d = _clamp_delta(deltas.get(key, 0))
        if d != 0:
            cur[axis] = _clamp(float(cur[axis]) + d * step)
            cur["idle_turns"][axis] = 0
        else:
            idle = int(cur["idle_turns"].get(axis, 0)) + 1
            cur["idle_turns"][axis] = idle
            if idle >= 2:
                val = float(cur[axis])
                if val > _NEUTRAL:
                    cur[axis] = _clamp(max(_NEUTRAL, val - 5.0))
                elif val < _NEUTRAL:
                    cur[axis] = _clamp(min(_NEUTRAL, val + 5.0))

    cur["turns"] = int(cur.get("turns", 0)) + 1
    reason = str(deltas.get("reason") or "neutral exchange").strip()
    cur["last_reason"] = reason[:120] if reason else "neutral exchange"
    cur["last_evaluator"] = evaluator[:24]
    cur["updated_at"] = time.time() if now is None else now
    return cur


def _evaluate_with_llm(ctx: Any, user_message: str, assistant_response: str) -> Optional[Dict[str, Any]]:
    snippet_user = " ".join(user_message.split())[:600]
    snippet_bot = " ".join((assistant_response or "").split())[:600]
    instructions = (
        "You evaluate conversational emotion shifts for a companion agent's 4 axes: "
        "energy (enthusiasm/length), warmth (closeness/friendliness), "
        "patience (tolerance vs annoyed/sulking from dry or rude replies), "
        "focus (technical/task seriousness vs playful distraction). "
        "Return JSON with integer deltas in {-1, 0, 1} for e_delta, w_delta, p_delta, f_delta, "
        "and a very short reason (max 6 words). Use 0 when an axis is unaffected."
    )
    prompt = f"User: {snippet_user}\nAssistant: {snippet_bot}"
    result = ctx.llm.complete_structured(
        instructions=instructions,
        input=[{"type": "text", "text": prompt}],
        json_schema=_EVALUATOR_SCHEMA,
        schema_name="mood_turn_delta",
        temperature=0.1,
        max_tokens=96,
        timeout=8.0,
        task="mood_eval",
        purpose="mood-engine.evaluate",
    )
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        return None
    return {
        "e_delta": _clamp_delta(parsed.get("e_delta", 0)),
        "w_delta": _clamp_delta(parsed.get("w_delta", 0)),
        "p_delta": _clamp_delta(parsed.get("p_delta", 0)),
        "f_delta": _clamp_delta(parsed.get("f_delta", 0)),
        "reason": str(parsed.get("reason") or "llm turn evaluation")[:120],
    }


def register(ctx: Any) -> None:
    state_lock = threading.Lock()

    def _load() -> Dict[str, Any]:
        raw = ctx.state.get("mood", default=None)
        state = _normalize_state(raw)
        return apply_time_drift(
            state,
            recovery_per_hour=float(ctx.get_config("recovery_per_hour", default=8)),
            night_fatigue=bool(ctx.get_config("enabled_night_fatigue", default=True)),
        )

    def _save(state: Dict[str, Any]) -> None:
        ctx.state.set("mood", state)

    def _is_user_turn(user_message: Any) -> bool:
        if not isinstance(user_message, str):
            return False
        stripped = user_message.strip()
        return bool(stripped) and not stripped.startswith(_INTERNAL_PREFIXES)

    def on_pre_llm_call(user_message: Any = None, **kwargs: Any) -> Optional[Dict[str, str]]:
        if not _is_user_turn(user_message):
            return None
        with state_lock:
            state = _load()
        inject_neutral = bool(ctx.get_config("inject_in_neutral", default=False))
        rendered = format_mood_context(state, inject_in_neutral=inject_neutral)
        return {"context": rendered} if rendered else None

    def _run_background_eval(user_msg: str, bot_reply: str, mode: str, step_size: float) -> None:
        deltas: Optional[Dict[str, Any]] = None
        evaluator_used = "heuristic"
        if mode in {"auto", "llm"}:
            try:
                deltas = _evaluate_with_llm(ctx, user_msg, bot_reply)
                if deltas is not None:
                    evaluator_used = "llm"
            except Exception as exc:
                logger.debug("mood-engine llm evaluation skipped: %s", exc)
        if deltas is None:
            if mode == "llm":
                return
            deltas = evaluate_heuristic_deltas(user_msg)
            evaluator_used = "heuristic"

        with state_lock:
            state = _load()
            updated = apply_turn_deltas(state, deltas, step_size=step_size, evaluator=evaluator_used)
            _save(updated)

    def on_post_llm_call(
        user_message: Any = None,
        assistant_response: Any = None,
        **kwargs: Any,
    ) -> None:
        if not _is_user_turn(user_message):
            return
        mode = str(ctx.get_config("evaluator_mode", default="auto") or "auto").strip().lower()
        if mode not in {"auto", "llm", "heuristic"}:
            mode = "auto"
        step_size = float(ctx.get_config("step_size", default=12))
        bot_reply = assistant_response if isinstance(assistant_response, str) else ""

        if mode == "heuristic":
            deltas = evaluate_heuristic_deltas(user_message)
            with state_lock:
                state = _load()
                updated = apply_turn_deltas(state, deltas, step_size=step_size, evaluator="heuristic")
                _save(updated)
            return

        # Copy active contextvars so multiplexed profile scope (_HERMES_HOME_OVERRIDE)
        # survives into the daemon thread for ctx.state, ctx.get_config, and ctx.llm.
        ctx_copy = contextvars.copy_context()
        worker = threading.Thread(
            target=ctx_copy.run,
            args=(_run_background_eval, user_message, bot_reply, mode, step_size),
            daemon=True,
        )
        worker.start()

    def handle_mood_command(raw_args: str) -> str:
        args = (raw_args or "").strip().lower().split()
        if not args or args[0] in {"status", "why"}:
            with state_lock:
                s = _load()
            mood = classify_mood(s)
            rules = build_directives(s)
            active = "; ".join(rules) if rules else "none (neutral zone)"
            return (
                f"Mood: {mood} | Energy: {int(round(s['energy']))} | "
                f"Warmth: {int(round(s['warmth']))} | Patience: {int(round(s['patience']))} | "
                f"Focus: {int(round(s['focus']))} | Turns: {s['turns']} | "
                f"Eval: {s['last_evaluator']} | Last: {s['last_reason']} | Directives: {active}"
            )
        if args[0] == "reset":
            with state_lock:
                fresh = _default_state()
                _save(fresh)
            return "Mood reset to neutral baseline (50/50/50/50)."
        if args[0] == "set" and len(args) >= 2 and args[1] in _PRESETS:
            preset = args[1]
            with state_lock:
                s = _load()
                s.update(_PRESETS[preset])
                s["idle_turns"] = {axis: 0 for axis in _AXES}
                s["last_reason"] = f"manual preset: {preset}"
                s["last_evaluator"] = "manual"
                s["updated_at"] = time.time()
                _save(s)
            return f"Mood set to {preset}."
        return f"Usage: /mood [status|why|reset|set <{'|'.join(_PRESETS)}>]"

    if hasattr(ctx, "register_auxiliary_task"):
        ctx.register_auxiliary_task(
            "mood_eval",
            display_name="Mood Engine Evaluator",
            description="Lightweight model for post-turn 4-axis mood evaluation (falls back to active model).",
            defaults={"provider": "auto", "model": "", "timeout": 8},
        )
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_command(
        "mood",
        handle_mood_command,
        description="Inspect, reset, or set the agent's 4-axis mood state",
        args_hint="[status|why|reset|set <preset>]",
    )
