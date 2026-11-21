# Contributing to Prior Art

Thank you for your interest in contributing to **Prior Art**!

## Local Development Setup

```bash
git clone https://github.com/coldrazer/priorArt.git
cd priorArt
python3.12 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.txt
```

You can run the full application and test suite offline with zero API keys:

```bash
PRIORART_FAKE=1 python src/agent.py serve
```

## Verification Gates (Required Before Opening a PR)

Every pull request must pass the same quality gates enforced in CI (`.github/workflows/ci.yml`):

```bash
python -m compileall -q src scripts tests
ruff check src scripts tests
mypy src/priorart
coverage run --source=src/priorart -m pytest -q
coverage report --fail-under=85
python scripts/db.py check
python scripts/explain_queries.py --owners 60 --reports-per-owner 3
```

## Database Migrations

Schema changes must follow [docs/MIGRATIONS.md](docs/MIGRATIONS.md):
- Never edit an already-applied migration (`0001`–`0005`).
- Add a sequential pair `migrations/NNNN_snake_name.up.sql` and `.down.sql` (or `.irreversible.md` with `## Why` and `## Recovery`).
- Keep `src/priorart/schema.sql` in exact sync and verify with `python scripts/db.py check`.
- Ensure every new query in `src/priorart/queries.py` uses an index (`scripts/explain_queries.py`).

## Security & Privacy Rules

- Never commit `.env` files, API keys, or real user data.
- Never interpolate third-party web text into LLM system prompts; keep untrusted content inside JSON payloads with `prompts.UNTRUSTED`.
- Keep `src/priorart/static/index.html` free of inline `style="..."` attributes so the dynamic SHA-256 CSP remains strict.
