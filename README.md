# Hermes Mood Engine

A lightweight, 4-axis dynamic emotional state plugin for [Hermes Agent](https://github.com/NousResearch/hermes-agent).

`mood-engine` tracks **Energy**, **Warmth**, **Patience**, and **Focus** across conversations and surfaces, evaluates emotional shifts asynchronously in the background after each turn (zero TTFT latency), and injects concise style directives only when an axis leaves the neutral zone.

## Features

- **4-axis state matrix (0-100, neutral at 50):**
  - **Energy** (`<=35` low-effort/brief, `>=65` lively/enthusiastic)
  - **Warmth** (`<=35` distant/matter-of-fact, `>=65` warm/playful)
  - **Patience** (`<45` sulking/short replies, `<20` heavy dry text until cheered up, `>=65` patient/reassuring)
  - **Focus** (`<=35` easily sidetracked/banter, `>=65` straight to technical substance)
- **Zero-token neutral zone:** When all axes sit in the neutral band, `pre_llm_call` returns nothing, keeping prompt overhead at zero.
- **Zero-latency background evaluation (`auto` mode):** Spawns a daemon thread in `post_llm_call` using `ctx.llm.complete_structured()` on your active Hermes provider, with automatic fallback to word-boundary heuristics if the LLM call is unavailable or disabled.
- **Natural decay & cross-session continuity:** State persists in `ctx.state` (`<HERMES_HOME>/plugin-data/`), drifts back toward neutral after consecutive neutral turns, and recovers gradually over idle hours.
- **In-chat controls:** `/mood`, `/mood reset`, and `/mood set <preset>`.

## Installation

```bash
hermes plugins install https://github.com/NiceStackApple/hermes-mood-engine
hermes plugins enable mood-engine
```

Or copy this folder to `~/.hermes/plugins/mood-engine` (or `$HERMES_HOME/plugins/mood-engine`) and run:

```bash
hermes plugins enable mood-engine
```

## Commands

- `/mood` (or `/mood status`) — View current axis scores, active directives, and last change reason.
- `/mood reset` — Reset all 4 axes to neutral (`50`).
- `/mood set <preset>` — Jump to a preset: `neutral`, `playful`, `warm`, `focused`, `tired`, `annoyed`, `badmood`.

## Configuration

Configure via **Settings → Plugins → mood-engine** in Hermes Desktop or `hermes config`:

| Setting | Default | Description |
|---|---|---|
| `evaluator_mode` | `auto` | `auto` (`ctx.llm` + heuristic fallback), `llm` (LLM only), or `heuristic` (zero extra tokens). |
| `step_size` | `12` | Score change per `-1`/`+1` turn delta (clamped to `1..30`). |
| `recovery_per_hour` | `8` | Idle recovery rate toward baseline `50` per hour. |
| `enabled_night_fatigue` | `true` | Apply a soft energy cap (`48`) between `23:00` and `05:00` local time. |
| `inject_in_neutral` | `false` | Inject a compact status line even when all axes are neutral. |

### Optional: Use a dedicated lightweight model (`auxiliary.mood_eval`)

By default (`provider: auto`), background evaluation uses whatever active model you already have configured — no extra setup needed, and fresh installs start at `50/50/50/50`.

To route background mood evaluation to a fast, cheap, or local model instead (e.g. Groq Llama, Gemini Flash-Lite, or local Ollama), configure the `mood_eval` auxiliary slot in Hermes Settings (**Auxiliary Models → Mood Engine Evaluator**) or CLI:

```bash
# Example: route to a fast model on your configured provider
hermes config set auxiliary.mood_eval.provider openrouter
hermes config set auxiliary.mood_eval.model qwen/qwen-2.5-7b-instruct

# Example: local Ollama
hermes config set auxiliary.mood_eval.provider custom
hermes config set auxiliary.mood_eval.base_url http://localhost:11434/v1
hermes config set auxiliary.mood_eval.model qwen2.5:1.5b
```

## Disclosures

- **LLM usage:** When `evaluator_mode` is `auto` or `llm`, each user turn triggers one bounded background `ctx.llm.complete_structured()` call (using your active Hermes provider/model) after the main reply is generated. Set `evaluator_mode` to `heuristic` for zero extra token usage.
- **Local storage:** Stores non-secret mood scores and the last short reason string in Hermes's profile-scoped `ctx.state` directory (`<HERMES_HOME>/plugin-data/`).
- **Network / shell:** Makes no direct HTTP requests and spawns no subprocesses.

## License

MIT
