"""Command line.

  python src/agent.py "your idea"                   # full report in the terminal, saved to runs/
  python src/agent.py "your idea" --md report.md    # also write a Markdown report with citations
  python src/agent.py --file idea.txt
  python src/agent.py "idea" --simulate search_down # demo the failure case
  python src/agent.py models                        # list Token Factory models your key can use
  python src/agent.py config check                  # validate and print effective settings (masked)
  python src/agent.py serve [--port 8000]           # web UI
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCORE = {0: "·", 1: "◐", 2: "●"}
COMMANDS = {"run", "models", "serve", "config"}


def _print_event(kind: str, d: dict[str, Any]) -> None:
    if kind == "stage":
        print(f"\n▸ {d['message']}")
    elif kind == "plan":
        for f in d["features"]:
            print(f"   {f['id']}  {f['name']}: {f['description']}")
    elif kind == "search":
        print(f"   {d['label']:<12} {d['count']:>2} results  {d['query'][:70]}")
    elif kind == "triage":
        for p in d["picked"]:
            print(f"   ✓ {p['name']}  ({p['url']})")
    elif kind == "verified":
        cut = d.get("cut_quote", 0) + d.get("cut_verifier", 0)
        extra = f", {d['unchecked']} unchecked" if d.get("unchecked") else ""
        print(f"   {d['name']:<28} {d['checked']} claims, {cut} cut, {d['downgraded']} downgraded{extra}")
    elif kind == "warn":
        print(f"   ! {d['message']}", file=sys.stderr)


def _cell_glyph(cell: dict[str, Any]) -> str:
    st = cell.get("status", "none")
    if st.startswith("cut"):
        return "✂"
    if st == "unchecked":
        return "?"
    if st in ("claimed", "unverified"):
        return "!"
    return SCORE.get(int(cell.get("score", 0)), "·")


def print_report(r: dict[str, Any]) -> None:
    feats = r["features"]
    print("\n" + "=" * 78)
    if r["novelty"] is None:
        print(f"NO SCORE ({r['status'].replace('_', ' ')})   ·   {r['summary']}")
    else:
        print(f"NOVELTY {r['novelty']}/100   ·   {r['summary']}")
    print("=" * 78)
    if r["message"]:
        print(f"⚠ {r['message']}")
    if r["projects"]:
        print()
        head = "".join(f"{f['id']:>4}" for f in feats)
        print(f"{'project':<30}{head}   source")
        for p in r["projects"]:
            row = "".join(f"{_cell_glyph(p['cells'][f['id']]):>4}" for f in feats)
            print(f"{('[' + str(p['n']) + '] ' + p['name'])[:29]:<30}{row}   {p['source_label']}")
        print(f"{'best coverage':<30}" + "".join(f"{SCORE[r['coverage'][f['id']]]:>4}" for f in feats))
        print("   ● done   ◐ partly   · open   ✂ rejected   ? unchecked   ! unverified")
        c = r["checks"]
        extras = []
        if c.get("unchecked"):
            extras.append(f"{c['unchecked']} unchecked")
        if c.get("unverified"):
            extras.append(f"{c['unverified']} unverified")
        extra_str = (" · " + " · ".join(extras)) if extras else ""
        print(
            f"\nChecks: {c['checked']} claims checked · {c['cut_quote']} cut (quote not on page) · "
            f"{c['cut_verifier']} cut by verifier · {c['downgraded']} downgraded{extra_str}"
        )
    if r["gaps"]:
        print("\nOpen gaps (nobody does these):")
        for g in r["gaps"]:
            print(f"   {g['id']} {g['name']}: {g['description']}")
    if r["verdict"]:
        print(f"\nVerdict: {r['verdict']}")
    for i, p in enumerate(r["pivots"], 1):
        print(f"  {i}. {p['title']}: {p.get('pitch', '')}  [{p.get('why_open', '')}]")
    if r["sources"]:
        print("\nSources:")
        for s in r["sources"]:
            print(f"  [{s['n']}] {s['url']}")
    saved = r.get("saved_to") or r.get("id") or "not saved"
    print(f"\nSaved: {saved}  ({r['seconds']}s)")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["--help"]
    if argv[0] not in COMMANDS and argv[0] not in ("-h", "--help"):
        argv.insert(0, "run")  # `agent.py "idea"` is shorthand for `agent.py run "idea"`

    ap = argparse.ArgumentParser(prog="agent.py", description="Prior Art: has someone built this already?")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="check an idea")
    run.add_argument("idea", nargs="?", help="the idea, in a sentence or a paragraph")
    run.add_argument("--file", help="read the idea from a text file")
    run.add_argument("--md", help="also write a Markdown report (with numbered citations) to this path")
    run.add_argument("--json", action="store_true", help="print the raw result as JSON")
    run.add_argument("--quiet", action="store_true", help="no progress output")
    run.add_argument("--simulate", choices=["search_down", "extract_down"], help="fake a Tavily outage (failure demo)")
    sub.add_parser("models", help="list models available to your Token Factory key")
    cfg = sub.add_parser("config", help="configuration utilities")
    cfg.add_argument("action", choices=["check"], help="validate and print effective settings (secrets masked)")
    srv = sub.add_parser("serve", help="start the web UI")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    from . import ConfigError, Settings, build, close_shared_runtime

    try:
        settings = Settings.from_env(validate=True)
    except ConfigError as exc:
        print(f"Configuration error:\n{exc}", file=sys.stderr)
        return 2

    if a.cmd == "config":
        print(json.dumps(settings.masked_summary(), indent=2))
        return 0

    if a.cmd == "models":
        if settings.fake:
            print("fake/primary-model\nfake/verifier-model")
            return 0
        from .cache import Cache
        from .llm import LLM

        llm = LLM(settings, Cache("", enabled=False))
        try:
            models = llm.list_models()
        except Exception as e:
            print(f"Couldn't list models: {e}", file=sys.stderr)
            return 1
        finally:
            llm.close()
        for m in models:
            print(("→ " if m in (settings.primary_model, settings.verifier_model) else "  ") + m)
        print()
        ok = True
        for role, m in (("writer  ", settings.primary_model), ("verifier", settings.verifier_model)):
            there = m in models
            ok &= there
            print(f"{role} {m}  {'✓ available' if there else '✗ NOT available: set it in .env'}")
        if settings.primary_model.split("/")[0].lower() == settings.verifier_model.split("/")[0].lower():
            print("note: writer and verifier are the same family. A different family makes a better checker.")
        return 0 if ok else 1

    if a.cmd == "serve":
        import uvicorn

        from .log import configure_logging

        configure_logging(env=settings.env)
        if a.host == "0.0.0.0":
            print(
                "Note: binding 0.0.0.0 — set PRIORART_ALLOWED_HOSTS to your domain so TrustedHostMiddleware accepts it.",
                file=sys.stderr,
            )
        print(f"Prior Art UI: http://{a.host}:{a.port}")
        uvicorn.run(
            "priorart.server:app",
            host=a.host,
            port=a.port,
            log_level="warning",
            access_log=False,
        )
        return 0

    if getattr(a, "simulate", None):
        settings.simulate = a.simulate
        try:
            settings.validate()
        except ConfigError as exc:
            print(f"Configuration error:\n{exc}", file=sys.stderr)
            return 2

    try:
        if a.file:
            with open(a.file, encoding="utf-8") as fh:
                idea = fh.read()
        else:
            idea = a.idea
    except OSError as e:
        print(f"Error reading {a.file}: {e}", file=sys.stderr)
        return 1

    if not idea:
        ap.error("give an idea, or --file")

    try:
        agent = build(settings, emit=None if (a.quiet or a.json) else _print_event)
        result = agent.run(idea)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        close_shared_runtime()

    if a.md:
        from .report import to_markdown

        md_path = Path(a.md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(to_markdown(result))
    if a.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_report(result)
        if a.md:
            print(f"Report: {a.md}")
    return 0 if result["status"] != "failed" else 2


if __name__ == "__main__":
    sys.exit(main())
