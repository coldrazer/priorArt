"""Markdown report with numbered citations: paste-ready for a README, a doc, or a judge."""
from __future__ import annotations

import re
from typing import Any

from .search import safe_url

MARK = {0: "·", 1: "◐ partly", 2: "● done"}
STATUS = {
    "ok": "**Note:** ",
    "no_close_match": "**Low evidence:** ",
    "insufficient_evidence": "**Insufficient evidence:** ",
    "failed": "**Run failed:** ",
}

_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-!|<>])")


def md_escape(text: Any, *, table: bool = False) -> str:
    """Escape Markdown/HTML control characters in untrusted web text so exports stay safe."""
    s = str(text or "")
    s = s.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    s = _MD_SPECIAL.sub(r"\\\1", s)
    if table:
        s = s.replace("\t", " ")
    return s.strip()


def _cell(c: dict[str, Any]) -> str:
    st = c.get("status", "none")
    if st in ("cut_quote", "cut_verifier"):
        return "~~cut~~"
    mark = MARK.get(int(c.get("score", 0)), "·")
    if st == "unchecked":
        return f"{mark} *(unchecked)*"
    if st in ("claimed", "unverified"):
        return f"{mark} *(unverified)*"
    return mark


def to_markdown(r: dict[str, Any]) -> str:
    out = ["# Prior Art report", "", f"**Idea:** {md_escape(r.get('idea', ''))}", ""]
    if r.get("demo"):
        out += ["> **Demo data:** generated with offline demo fixtures, not live search results.", ""]
    if r.get("message"):
        prefix = STATUS.get(str(r.get("status", "")), "**Note:** ")
        out += [prefix + md_escape(r["message"]), ""]
    if r.get("novelty") is not None:
        verdict = md_escape(r.get("verdict", ""))
        out += [f"**Novelty: {r['novelty']}/100**" + (f" · {verdict}" if verdict else ""), ""]

    feats = r.get("features") or []
    out += (
        ["## Features checked", ""]
        + [f"- **{md_escape(f.get('id', ''))} {md_escape(f.get('name', ''))}**: {md_escape(f.get('description', ''))}" for f in feats]
        + [""]
    )

    projects = r.get("projects") or []
    coverage = r.get("coverage") or {}
    if projects:
        out += [
            "## Overlap grid",
            "",
            "| Existing project | " + " | ".join(md_escape(f.get("name", ""), table=True) for f in feats) + " |",
            "|---|" + "---|" * len(feats),
        ]
        for p in projects:
            cells = p.get("cells") or {}
            row = " | ".join(_cell(cells.get(f["id"], {"score": 0, "status": "none"})) for f in feats)
            out.append(f"| {md_escape(p.get('name', ''), table=True)} [{p.get('n', '')}] | {row} |")
        out += [
            "| **Best coverage** | "
            + " | ".join(
                MARK.get(int(coverage.get(f["id"], 0)), "·") if coverage.get(f["id"], 0) else "**open**"
                for f in feats
            )
            + " |",
            "",
        ]

        out += ["## Evidence", ""]
        ev_lines: list[str] = []
        for p in projects:
            cells = p.get("cells") or {}
            for f in feats:
                c = cells.get(f["id"], {})
                st = c.get("status")
                if st in ("verified", "downgraded", "claimed", "unverified", "unchecked"):
                    suffix = ""
                    if st == "unchecked":
                        suffix = " *(not independently checked)*"
                    elif st in ("claimed", "unverified"):
                        suffix = " *(unverified — verifier unavailable)*"
                    ev_lines.append(
                        f"- **{md_escape(p.get('name', ''))} → {md_escape(f.get('name', ''))}** "
                        f"({MARK.get(int(c.get('score', 0)), '·')}{suffix}): “{md_escape(c.get('quote', ''))}” [{p.get('n', '')}]"
                    )
        out += ev_lines or ["- None."]
        out.append("")

        cut = [
            (p, f, p.get("cells", {}).get(f["id"], {}))
            for p in projects
            for f in feats
            if p.get("cells", {}).get(f["id"], {}).get("status") in ("cut_quote", "cut_verifier")
        ]
        ch = r.get("checks") or {}
        extra_counts = []
        if ch.get("unchecked"):
            extra_counts.append(f"{ch['unchecked']} unchecked (verifier omitted)")
        if ch.get("unverified"):
            extra_counts.append(f"{ch['unverified']} unverified (verifier unavailable)")
        extra_str = (" · " + " · ".join(extra_counts)) if extra_counts else ""
        out += [
            "## Claims the checks threw out",
            "",
            f"{ch.get('checked', 0)} overlap claims checked · {ch.get('cut_quote', 0)} cut because the quote wasn't on the page · "
            f"{ch.get('cut_verifier', 0)} cut by the verifier model · {ch.get('downgraded', 0)} downgraded{extra_str}",
            "",
        ]
        out += [
            f"- ~~{md_escape(p.get('name', ''))} → {md_escape(f.get('name', ''))}~~: "
            f"“{md_escape(c.get('quote', ''))}” ({md_escape(c.get('reason', ''))}) [{p.get('n', '')}]"
            for p, f, c in cut
        ] or ["- None."]
        out.append("")

    gaps = r.get("gaps") or []
    if gaps:
        out += (
            ["## Open gaps", ""]
            + [f"- **{md_escape(g.get('name', ''))}**: {md_escape(g.get('description', ''))}" for g in gaps]
            + [""]
        )

    pivots = r.get("pivots") or []
    if pivots:
        out += (
            ["## Pivots", ""]
            + [
                f"{i}. **{md_escape(p.get('title', ''))}**: {md_escape(p.get('pitch', ''))} _{md_escape(p.get('why_open', ''))}_"
                for i, p in enumerate(pivots, 1)
            ]
            + [""]
        )

    sources = r.get("sources") or []
    if sources:
        src_lines = []
        for s in sources:
            url = safe_url(s.get("url"))
            url_part = f"<{url}>" if url else "(invalid URL omitted)"
            src_lines.append(f"[{s.get('n', '')}] {md_escape(s.get('name', ''))} ({md_escape(s.get('source', ''))}): {url_part}")
        out += ["## Sources", ""] + src_lines + [""]

    models = r.get("models") or {}
    out += [
        f"_{r.get('candidates_found', 0)} search results screened · models: {md_escape(models.get('primary', 'unknown'))} (writer), "
        f"{md_escape(models.get('verifier', 'unknown'))} (verifier) · search: Tavily · {r.get('seconds', 0)}s_",
        "",
    ]
    return "\n".join(out)
