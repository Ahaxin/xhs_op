# xhs_op

Semi-automated content engine for two Xiaohongshu accounts:

- `@banna-villa` — Xishuangbanna villa / apartment rentals.
- `@stock-ai-digest` — AI / crypto / stock digests funneling to deep-dive reports.

AI drafts everything, a human approves in a local Streamlit dashboard before any post goes live. Everything runs locally on Windows.

## Setup

```powershell
# 1. Install uv (https://docs.astral.sh/uv/) if you haven't.
# 2. From the project root:
uv sync
uv run playwright install chromium

# 3. Copy .env.example to .env and fill in keys + proxies.
cp .env.example .env

# 4. Initialize the SQLite DB.
uv run python -c "from xhs_op.db import init_db; init_db()"

# 5. Per-account QR-code login (Playwright opens a real Chromium window):
uv run python scripts/login.py --account banna
uv run python scripts/login.py --account stock
```

## Plan

The full architecture, task breakdown, and acceptance criteria live in the plan file:
`~/.claude/plans/now-i-have-a-federated-dragonfly.md`.

Schema and inter-task contracts are locked in `src/xhs_op/db.py`.
