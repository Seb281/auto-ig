# auto-ig — Project Instructions

Autonomous Instagram post creator. AI pipeline that generates and publishes single-image posts, controlled via Discord.

## Agent workflow

This project uses 4 specialized agents in `.claude/agents/`:

- **auto-ig-planner** — Read-only. Plans a milestone. Invoke first.
- **auto-ig-builder** — Implements code. Invoke with planner output.
- **auto-ig-reviewer** — Read-only. Reviews code, returns PASS/FAIL. Only CRITICAL issues cause FAIL.
- **auto-ig-milestone** — Orchestrator. Chains planner -> builder -> reviewer automatically with retry.

To run a milestone end-to-end: invoke `auto-ig-milestone` with "Run Milestone N".

## Banned (never use these)

- `instagrapi` — TOS violation, use Meta Graph API
- `requests` — blocking, use `httpx.AsyncClient`
- `sqlite3` in async context — use `aiosqlite`
- `BackgroundScheduler` — use `AsyncIOScheduler`
- `python-telegram-bot` — replaced by `discord.py`
- `genai.Client()` without `api_key` — always pass `api_key=os.getenv("GEMINI_API_KEY")`
- Hardcoded secrets — all via `os.getenv()` after `load_dotenv()`
- `print()` for logging — use `logging.getLogger(__name__)`
- `git add -A` or `git add .` — stage specific files only

## Required patterns

- All I/O functions: `async def` + `await`
- DB queries: parameterized (`?` placeholders), never f-strings
- Temp images: deleted in `finally` block
- Config: `AccountConfig` dataclass passed as argument, never re-read from disk
- Type hints on all public function signatures
- One-line docstring on all public functions
- Every Python package directory must have `__init__.py`

## Tech stack

Python 3.11+, `google-genai` SDK (Gemini 2.0 Flash + 2.5 Flash Image, via `utils/ai_client.py`), `discord.py` 2.x, `APScheduler` (AsyncIOScheduler), `aiosqlite`, `httpx`, `Pillow`, `imagehash`, `PyYAML`, `python-dotenv`
