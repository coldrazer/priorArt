## Summary
<!-- Briefly describe what this pull request changes and why. -->

## Verification Checklist
- [ ] `python -m compileall -q src scripts tests`
- [ ] `ruff check src scripts tests`
- [ ] `mypy src/priorart`
- [ ] `coverage run --source=src/priorart -m pytest -q && coverage report --fail-under=85`
- [ ] `python scripts/db.py check` (if schema/migrations changed)
- [ ] `python scripts/explain_queries.py --owners 60 --reports-per-owner 3` (if queries/indexes changed)
- [ ] No secrets, `.env` files, or raw user data committed
