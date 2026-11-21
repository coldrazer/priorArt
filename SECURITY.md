# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in **Prior Art**, please report it responsibly rather than opening a public issue:

- Open a private security advisory on GitHub at **https://github.com/coldrazer/priorArt/security/advisories/new**, or
- Contact the maintainer ([@coldrazer](https://github.com/coldrazer)) directly with reproduction details and impact.

We aim to acknowledge reports within 48 hours and publish fixes promptly.

## Security Architecture

Prior Art implements defense-in-depth across its LLM pipeline, web server, and persistence layer:

| Control | Implementation |
|---|---|
| **Prompt-Injection Defense** | Web content arrives strictly inside JSON payloads wrapped in per-request random `<<<PAGE …>>>` boundaries; every web-facing prompt includes `UNTRUSTED DATA`; every overlap claim must pass both deterministic code quote matching (`quote_in_page`) and an independent verifier from a different model family. |
| **CSRF & Paid-Work Guard** | `GET /api/run` is retired (`405`). Checks start only via `POST /api/checks` with `Sec-Fetch-Site` / `Origin` enforcement, `Idempotency-Key` deduplication, SQLite token-bucket rate limits, concurrency caps, and daily token/credit spend ceilings. |
| **Owner Data Isolation** | Anonymous 256-bit `HttpOnly` cookies (`__Host-pa_owner` over HTTPS/production, `pa_owner` on local HTTP) stored strictly as SHA-256 hashes; all report reads, streams, exports, and deletes return `404` for non-owners. |
| **Browser & Export Hardening** | Dynamic SHA-256 Content-Security-Policy (`frame-ancestors 'none'`, zero `unsafe-inline`), `TrustedHostMiddleware` (DNS rebinding protection), `nosniff`, `Referrer-Policy: no-referrer`, HSTS in production, `http:`/`https:` URL scheme allowlist, `rel="noopener noreferrer"`, and full Markdown escaping in `report.py`. |
| **Secret & Log Redaction** | Structured JSON logs automatically redact API keys, tokens, cookies, emails, and user idea text; clients receive opaque error references (`err_…`) instead of raw exception traces. |
| **Supply Chain** | Universal SHA-256 hashed lockfiles (`pip install --require-hashes`), GitHub Actions pinned to commit SHAs with `permissions: contents: read`, and Dependabot enabled. |
