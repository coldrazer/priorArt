"""Submission helper for the ODSC × Nebius hackathon.

  python scripts/submission.py fill --video https://youtu.be/xxxx   # fill the README from your latest live run
  python scripts/submission.py check                                # run the submission checklist

fill   takes the most recent successful live run in runs/ (or --run FILE), writes docs/example-report.md,
       and fills the README's Example section, model names, clone URL and demo video link.
check  verifies every checklist item it can from here, runs the tests, and prints the time left.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
README = ROOT / "README.md"
EXAMPLE = ROOT / "docs" / "example-report.md"
DEADLINE = datetime(2026, 11, 21, 4, 59, tzinfo=timezone.utc)  # Nov 20, 2026, 11:59 PM EST
MARK = {0: "·", 1: "◐", 2: "●"}


# ---------- fill -------------------------------------------------------------------------------

def latest_live_run(path: str | None) -> tuple[Path, dict]:
    if path:
        p = Path(path)
        return p, json.loads(p.read_text())
    runs_dir = Path(os.getenv("PRIORART_RUNS_DIR", str(ROOT / "runs")))
    for p in sorted(runs_dir.glob("*.json"), reverse=True):
        r = json.loads(p.read_text())
        if r.get("status") == "ok" and not r["models"]["primary"].startswith("fake/"):
            return p, r
    sys.exit('No successful live run in runs/ yet. Run one first, e.g.\n  python src/agent.py "Build a research agent."')


def services_block(r: dict) -> str:
    m = r["models"]
    return f"""| Requirement | How we use it |
|---|---|
| Nebius Token Factory (inference) | Two models on the OpenAI-compatible API. **Writer** `{m['primary']}`: breaks the idea into features, plans the queries, screens results, compares each project with quotes, suggests pivots. **Verifier** `{m['verifier']}` (a different model family): checks every overlap claim against its quote. |
| Tavily (real-time web search) | `search` (advanced depth, domain-filtered per source type: GitHub, Hacker News, Product Hunt, arXiv, Devpost, open web) finds candidates; `extract` reads the full pages. Every claim keeps its URL and comes out as a numbered [n] source in the grid, the verdict, the pivots and the Markdown report. Quotes are checked against the extracted page text. |"""


def setup_block(repo_url: str) -> str:
    folder = repo_url.rstrip("/").split("/")[-1].removesuffix(".git") or "prior-art"
    return f"""```bash
git clone {repo_url}
cd {folder}
python -m venv .venv && source .venv/bin/activate     # Python 3.10+
pip install -r requirements.txt
cp .env.example .env   # add your keys — never commit them
python src/agent.py models                            # confirms your two models are available
python src/agent.py "Your research question here"     # e.g. "Has anyone built a scam-call detector for aging parents?"
python src/agent.py serve                             # web UI at http://127.0.0.1:8000
```"""


def example_block(r: dict) -> str:
    feats = r["features"]
    out = [f"**Question:** {r['idea']}", "",
           f"**Answer (excerpt):** **Novelty {r['novelty']}/100.** {r['verdict']}", "",
           "| Closest projects | " + " | ".join(f["name"] for f in feats) + " |",
           "|---|" + "---|" * len(feats)]
    for p in r["projects"][:3]:
        cells = ["~~cut~~" if p["cells"][f["id"]]["status"].startswith("cut") else MARK[p["cells"][f["id"]]["score"]]
                 for f in feats]
        out.append(f"| {p['name']} [{p['n']}] | " + " | ".join(cells) + " |")
    out += ["", "● done · ◐ partly · · open · ~~cut~~ = claim thrown out by the checks", ""]
    ev = [(p, f, p["cells"][f["id"]]) for p in r["projects"] for f in feats
          if p["cells"][f["id"]]["status"] == "verified" and p["cells"][f["id"]]["score"] == 2][:3]
    if ev:
        out += ["Evidence behind the top overlaps:"]
        out += [f"- **{p['name']} → {f['name']}**: “{c['quote']}” [{p['n']}]" for p, f, c in ev]
        out.append("")
    ch = r["checks"]
    out.append(f"Checks: {ch['checked']} overlap claims checked; {ch['cut_quote']} cut because the quote wasn't on "
               f"the page, {ch['cut_verifier']} cut by the verifier, {ch['downgraded']} downgraded.")
    if r["gaps"]:
        out += ["", "Open gaps: " + ", ".join(f"**{g['name']}**" for g in r["gaps"])]
    out += ["", "**Sources:** " + " · ".join(f"[{s['n']}] {s['url']}" for s in r["sources"][:6]), "",
            "Full report, with every quote and every cut claim: [docs/example-report.md](docs/example-report.md)"]
    return "\n".join(out)


def replace_block(text: str, name: str, body: str) -> str:
    pat = re.compile(rf"(<!-- AUTO:{name} -->\n).*?(\n<!-- /AUTO:{name} -->)", re.S)
    if not pat.search(text):
        sys.exit(f"README.md is missing its <!-- AUTO:{name} --> markers")
    return pat.sub(lambda m: m.group(1) + body.strip("\n") + m.group(2), text)


def git_remote() -> str | None:
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    m = re.match(r"git@github\.com:(.+?)(\.git)?$", url)
    return f"https://github.com/{m.group(1)}" if m else url.removesuffix(".git")


def fill(a) -> int:
    from priorart.report import to_markdown

    path, r = latest_live_run(a.run)
    EXAMPLE.parent.mkdir(exist_ok=True)
    EXAMPLE.write_text(to_markdown(r))
    text = README.read_text()
    text = replace_block(text, "services", services_block(r))
    text = replace_block(text, "example", example_block(r))
    repo = a.repo or git_remote()
    if repo:
        text = replace_block(text, "setup", setup_block(repo))
    if a.video:
        text = re.sub(r"(- \*\*Demo video \(2–3 min\):\*\*).*", lambda m: f"{m.group(1)} {a.video}", text)
    README.write_text(text)
    print(f"Filled README from {path.name}\nWrote {EXAMPLE.relative_to(ROOT)}")
    if not repo:
        print("Clone URL not set (no git remote yet). Re-run with --repo https://github.com/you/prior-art")
    if not a.video:
        print("Demo video link not set. Re-run with --video <url> once it's uploaded.")
    return 0


# ---------- check ------------------------------------------------------------------------------

def check(a) -> int:
    items: list[tuple[bool | None, str, str]] = []

    def item(ok, label, fix=""):
        items.append((ok, label, fix))

    readme = README.read_text()
    todos = re.findall(r"TODO[^\n|]{0,50}", readme)
    item(not todos, "README filled in, no TODOs left", " / ".join(t.strip() for t in todos[:3]))
    m = re.search(r"\*\*Demo video \(2–3 min\):\*\*\s*(\S+)", readme)
    item(bool(m and m.group(1).startswith("http")), "Demo video linked in README",
         "python scripts/submission.py fill --video <url>")
    item(EXAMPLE.exists() and "fake/" not in EXAMPLE.read_text(), "Example report from a live run (docs/example-report.md)",
         "run a live idea, then: python scripts/submission.py fill")

    is_git = (ROOT / ".git").exists()
    if is_git:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        item(".env" not in tracked, ".env is not committed", "git rm --cached .env && commit")
        ignored = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=ROOT).returncode == 0
        item(ignored, ".env is git-ignored", "add .env to .gitignore")
        files = [ROOT / f for f in tracked]
    else:
        item(False, "Project is a git repo pushed to GitHub", "git init, commit, push to a public repo")
        skip = {".git", ".venv", "runs", ".cache", "__pycache__", ".pytest_cache"}
        files = [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.parts)]

    secrets: list[str] = []
    try:
        from dotenv import dotenv_values

        secrets = [v for k, v in dotenv_values(ROOT / ".env").items() if k.endswith("API_KEY") and v and len(v) > 8]
    except ImportError:
        pass
    leaked = [str(f.relative_to(ROOT)) for f in files
              if f.is_file() and f.name != ".env" and f.stat().st_size < 2_000_000
              and any(s in f.read_text(errors="ignore") for s in secrets)]
    item(not leaked, "No API keys in repo files" + ("" if secrets else " (no .env keys to compare against)"),
         ", ".join(leaked))

    if is_git and shutil.which("gh"):
        vis = subprocess.run(["gh", "repo", "view", "--json", "visibility", "-q", ".visibility"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
        if vis:
            item(vis == "PUBLIC", "GitHub repo is public", f"now {vis}: gh repo edit --visibility public")
        else:
            item(None, "GitHub repo is public (no GitHub remote found yet; push first, then re-check)")
    else:
        item(None, "GitHub repo is public (can't check from here; open it in a private window to confirm)")

    if not a.skip_tests:
        t = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
        last = (t.stdout.strip().splitlines() or ["no output"])[-1]
        item(t.returncode == 0, f"Tests pass ({last})", "python -m pytest -q to see failures")

    item(None, "Inference on Token Factory, search on Tavily (built in)")
    item(None, "Submitted via the link emailed to registered participants (manual)")

    for ok, label, fix in items:
        mark = "✓" if ok else ("✗" if ok is False else "·")
        print(f" {mark} {label}" + (f"\n     → {fix}" if fix and ok is False else ""))
    left = DEADLINE - datetime.now(timezone.utc)
    hrs = left.total_seconds() / 3600
    when = "Nov 20, 11:59 PM EST (8:59 PM PT)"
    print(f"\n {'⏱ ' + f'{int(hrs)}h {int(left.total_seconds() % 3600 // 60)}m left' if hrs > 0 else '⚠ deadline passed'}"
          f" · deadline {when}")
    return 1 if any(ok is False for ok, _, _ in items) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Prior Art submission helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fill", help="fill the README from your latest live run")
    f.add_argument("--video", help="demo video URL")
    f.add_argument("--repo", help="public repo URL (default: git remote origin)")
    f.add_argument("--run", help="use this run JSON instead of the latest live run")
    c = sub.add_parser("check", help="run the submission checklist")
    c.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    return fill(a) if a.cmd == "fill" else check(a)


if __name__ == "__main__":
    sys.exit(main())
