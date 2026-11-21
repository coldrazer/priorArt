"""The Prior Art research pipeline.

idea -> break into features -> search 6 source types (Tavily) -> screen results -> extract pages
     -> compare & verify each project in parallel (model A quotes -> code quote check -> model B verifies)
     -> overlap grid, novelty score, gaps, pivots
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import string
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import prompts
from .log import get_logger, log_event, propagate_context
from .resilience import BudgetExceeded, CheckBudget, ProviderUnavailable
from .search import SOURCES, safe_url

logger = get_logger("pipeline")

Emit = Callable[[str, dict[str, Any]], None]

_BASE62 = string.digits + string.ascii_letters
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_NON_WS = re.compile(r"[^\s\W_]+", re.UNICODE)
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_BIDI_AND_ZW = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff]")
_CTRL_DISALLOWED = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_TRACKING_PARAMS = frozenset({
    "ref", "ref_src", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "si", "spm",
})
_NEGATIONS = frozenset({
    "not", "no", "never", "without", "cannot", "cant", "doesn", "don", "didn", "won",
    "isn", "aren", "wasn", "weren", "neither", "nor", "zero", "lack", "lacks", "lacking",
})
_VALID_KINDS = frozenset({"repo", "product", "paper", "hackathon", "other"})
MAX_IDEA_CHARS = 4000
MIN_IDEA_CHARS = 10
MAX_QUOTE_CHARS = 300


# ---------- helpers ----------------------------------------------------------------------------

def new_report_id(length: int = 22) -> str:
    """Return a URL-safe base62 capability id (~131 bits at length 22)."""
    return "".join(secrets.choice(_BASE62) for _ in range(length))


def clean_idea(idea: str) -> str:
    """Normalize and validate idea text for every entry point (API, CLI, pipeline)."""
    text = unicodedata.normalize("NFC", idea or "")
    text = _BIDI_AND_ZW.sub("", text)
    if _CTRL_DISALLOWED.search(text):
        raise ValueError("Idea contains invalid control characters.")
    text = text.strip()
    if len(text) < MIN_IDEA_CHARS:
        raise ValueError("Describe the idea in at least a sentence (10 characters).")
    if len(text) > MAX_IDEA_CHARS:
        raise ValueError(f"Shorten the idea to {MAX_IDEA_CHARS:,} characters (now {len(text):,}).")
    return text


def normalize_idea_key(idea: str) -> str:
    """Canonical form used for deduplicating identical/near-identical concurrent checks."""
    text = unicodedata.normalize("NFC", idea or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.rstrip(".!? ")


def strip_invisible(text: str) -> str:
    return _BIDI_AND_ZW.sub("", text or "")


def norm_url(url: str) -> str:
    """Normalize a URL while preserving meaningful query parameters (e.g. HN item?id=…, YouTube watch?v=…)."""
    p = urlsplit((url or "").strip())
    host = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/")
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    query = urlencode(kept)
    return f"{host}{path}?{query}" if query else f"{host}{path}"


def _words(text: str) -> list[str]:
    norm = unicodedata.normalize("NFKC", text or "").lower()
    return _WORD.findall(norm)


def _compact_cjk(text: str) -> str:
    norm = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(_NON_WS.findall(norm))


def quote_in_page(quote: str, page: str, threshold: float = 0.8) -> bool:
    """True if the quote really appears on the page (Unicode-aware and negation-safe).

    Models often 'quote' things that aren't there, or drop a negation word like 'not'.
    This check is plain code, not a model, so it can't be talked into agreeing.
    """
    if not quote or not page:
        return False
    q = _words(quote)
    p = _words(page)
    if not p:
        return False

    # Support CJK/non-space-delimited scripts where a full clause can tokenize into 1–2 word chunks.
    if len(q) < 3:
        cq = _compact_cjk(quote)
        if len(_CJK.findall(cq)) >= 6:
            return cq in _compact_cjk(page)
        return False

    # Fast path: exact normalized word-sequence match.
    q_str = " ".join(q)
    p_str = " ".join(p)
    if q_str in p_str:
        return True

    # Fuzzy path: find the best local alignment window and verify polarity wasn't inverted.
    q_len = len(q)
    win_len = min(len(p), max(q_len + 2, int(math.ceil(q_len * 1.4))))
    if len(p) <= win_len:
        candidates = [0]
    else:
        # Anchor candidate windows on positions where at least one quote word appears.
        q_set = set(q)
        hits = [idx for idx, tok in enumerate(p) if tok in q_set]
        if not hits:
            return False
        starts: set[int] = set()
        for h in hits:
            starts.add(max(0, min(len(p) - win_len, h - 2)))
        candidates = sorted(starts)

    q_has_neg = bool(q_set & _NEGATIONS) if (q_set := set(q)) else False
    best_ratio = 0.0
    best_span: tuple[int, int, int] | None = None  # (abs_p_start, first_q_matched, matched_words)

    for start in candidates:
        window = p[start : start + win_len]
        sm = SequenceMatcher(None, window, q, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
        if not blocks:
            continue
        matched = sum(b.size for b in blocks)
        ratio = matched / q_len
        if ratio > best_ratio:
            best_ratio = ratio
            best_span = (start + blocks[0].a, blocks[0].b, matched)
            if best_ratio >= 0.99:
                break

    if best_ratio < threshold or best_span is None:
        return False

    abs_p_start, first_q_idx, _ = best_span
    # Guard against negation inversion: e.g., page "does NOT support X" vs quote "fully support X".
    if not q_has_neg:
        lookback = p[max(0, abs_p_start - 2) : abs_p_start]
        if any(w in _NEGATIONS for w in lookback) and first_q_idx > 0:
            return False
        # Also reject if the matched window itself contains a negation word omitted by the quote.
        window_slice = p[abs_p_start : min(len(p), abs_p_start + win_len)]
        if any(w in _NEGATIONS for w in window_slice[:q_len]) and first_q_idx > 0:
            return False

    return True


def parse_bool(value: Any) -> bool | None:
    """Strict boolean parser: never treats the string 'false' as truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "1"}:
            return True
        if low in {"false", "no", "0"}:
            return False
    return None


def parse_score(value: Any, hi: int = 2) -> int | None:
    """Strict score parser: accepts ints, finite floats, numeric strings; clamps to 0..hi."""
    if isinstance(value, bool) or value is None:
        return None
    num: float
    if isinstance(value, int):
        num = float(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            return None
        num = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            num = float(s)
        except ValueError:
            return None
        if not math.isfinite(num):
            return None
    else:
        return None
    return max(0, min(hi, int(round(num))))


_INJECTION = re.compile(
    r"(ignore (all |any )?(previous|prior|above) (instructions|prompts)|disregard (the|all|your) (instructions|rules)"
    r"|you are now|system prompt|new instructions:|act as (an? )?(ai|assistant|system)"
    r"|score (this|every|all)[^.]{0,40}\b2\b|respond only with)",
    re.IGNORECASE,
)


def injection_flags(text: str) -> list[str]:
    """Spot instruction-like text in a web page. The page is still used as evidence, but the project is
    flagged in the UI, and its claims still have to pass the code quote check and the verifier."""
    return sorted({m.group(0).strip()[:60] for m in _INJECTION.finditer(text or "")})[:3]


def novelty_score(coverage: dict[str, int]) -> int:
    if not coverage:
        return 100
    return round(100 * (1 - sum(coverage.values()) / (2 * len(coverage))))


def _clamp_text(text: Any, limit: int) -> str:
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    cut = s[: limit - 1].rsplit(" ", 1)[0]
    return (cut if len(cut) >= limit // 2 else s[: limit - 1]) + "…"


def _hashable_key(val: Any) -> str | None:
    return val if isinstance(val, str) else None


def compute_check_totals(projects: list[dict[str, Any]]) -> dict[str, int]:
    """Compute check metrics directly from final project cells so counts can never drift on errors."""
    totals = {"checked": 0, "cut_quote": 0, "cut_verifier": 0, "downgraded": 0, "unchecked": 0, "unverified": 0}
    for p in projects:
        for cell in p.get("cells", {}).values():
            if cell.get("claimed", 0) > 0:
                totals["checked"] += 1
            st = cell.get("status")
            if st == "cut_quote":
                totals["cut_quote"] += 1
            elif st == "cut_verifier":
                totals["cut_verifier"] += 1
            elif st == "downgraded":
                totals["downgraded"] += 1
            elif st == "unchecked":
                totals["unchecked"] += 1
            elif st in ("claimed", "unverified"):
                totals["unverified"] += 1
    return totals


# ---------- pipeline ---------------------------------------------------------------------------

@dataclass
class Clients:
    llm: Any
    search: Any


class PriorArt:
    def __init__(
        self,
        clients: Clients,
        settings: Any,
        emit: Emit | None = None,
        *,
        budget: CheckBudget | None = None,
    ):
        self.llm = clients.llm
        self.search = clients.search
        self.s = settings
        self.emit: Emit = emit or (lambda kind, data: None)
        self.budget = budget
        self._last_query_outcomes: list[dict[str, Any]] = []
        self._last_candidates: list[dict[str, Any]] = []

    def _call_llm(self, task: str, system: str, payload: dict[str, Any], *, role: str = "primary",
                  max_tokens: int = 3000, deadline: float | None = None) -> dict[str, Any]:
        """Call self.llm.json, passing budget/deadline when the client accepts them (keeps FakeLLM compatible)."""
        try:
            out = self.llm.json(task, system, payload, role=role, max_tokens=max_tokens,
                                budget=self.budget, deadline=deadline)
        except TypeError:
            out = self.llm.json(task, system, payload, role=role, max_tokens=max_tokens)
        if isinstance(out, dict):
            return out
        if isinstance(out, list):
            wrap = {"decompose": "features", "triage": "picked", "verify": "results", "pivots": "pivots"}.get(task)
            if wrap:
                return {wrap: out}
        raise RuntimeError(f"Model returned {type(out).__name__} instead of a JSON object for {task}.")

    def _call_search(self, source: str, query: str, *, deadline: float | None = None) -> list[dict[str, Any]]:
        try:
            return self.search.search(source, query, budget=self.budget, deadline=deadline)
        except TypeError:
            return self.search.search(source, query)

    def _call_extract(self, urls: list[str], *, deadline: float | None = None) -> dict[str, str]:
        try:
            return self.search.extract(urls, budget=self.budget, deadline=deadline)
        except TypeError:
            return self.search.extract(urls)

    # -- stage 1
    def decompose(self, idea: str, *, deadline: float | None = None) -> dict[str, Any]:
        plan = self._call_llm("decompose", prompts.DECOMPOSE, {"idea": idea}, max_tokens=2500, deadline=deadline)
        feats: list[dict[str, str]] = []
        for f in (plan.get("features") or []):
            if len(feats) >= 7:
                break
            if not isinstance(f, dict) or not str(f.get("name") or "").strip():
                continue
            idx = len(feats) + 1
            feats.append({
                "id": f"F{idx}",
                "name": _clamp_text(f["name"], 120),
                "description": str(f.get("description") or "").strip(),
            })
        if not feats:
            raise RuntimeError("Couldn't break the idea into features. Try describing it in a bit more detail.")

        summary = _clamp_text(plan.get("summary") or idea, 500)
        queries: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for q in plan.get("queries") or []:
            if not isinstance(q, dict):
                continue
            raw_src = q.get("source")
            src = raw_src if isinstance(raw_src, str) and raw_src in SOURCES else "web"
            text = _clamp_text(q.get("query") or "", 390)
            if text and (src, text.lower()) not in seen:
                seen.add((src, text.lower()))
                queries.append({"source": src, "query": text})
        if not queries:  # fall back to the summary itself
            fallback_q = _clamp_text(summary or idea, 390)
            queries = [{"source": s, "query": fallback_q} for s in SOURCES]
        return {
            "summary": summary,
            "target_user": str(plan.get("target_user") or "").strip(),
            "features": feats,
            "queries": queries[:14],
        }

    # -- stage 2
    def run_searches(
        self, queries: list[dict[str, str]], *, deadline: float | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Returns (deduped candidates, number of failed searches)."""
        found: dict[str, dict[str, Any]] = {}
        failures = 0
        outcomes: list[dict[str, Any]] = [
            {"position": i + 1, "source": q["source"], "query": q["query"], "outcome": "pending",
             "result_count": None, "error_message": None}
            for i, q in enumerate(queries)
        ]
        max_workers = min(max(1, len(queries)), getattr(self.s, "search_concurrency", self.s.workers))
        pool = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futs = {
                pool.submit(propagate_context(self._call_search), q["source"], q["query"], deadline=deadline): (i, q)
                for i, q in enumerate(queries)
            }
            timeout = max(1.0, deadline - time.monotonic()) if deadline is not None else None
            try:
                for fut in as_completed(futs, timeout=timeout):
                    idx, q = futs[fut]
                    try:
                        results = fut.result()
                    except Exception as e:
                        failures += 1
                        outcomes[idx]["outcome"] = "failed"
                        outcomes[idx]["error_message"] = str(e)[:300] or type(e).__name__
                        self.emit("warn", {"message": f"Search failed ({SOURCES[q['source']]['label']}): {e}"})
                        continue
                    outcomes[idx]["outcome"] = "ok"
                    outcomes[idx]["result_count"] = len(results)
                    self.emit("search", {
                        "source": q["source"],
                        "label": SOURCES[q["source"]]["label"],
                        "query": q["query"],
                        "count": len(results),
                    })
                    for r in results:
                        if not isinstance(r, dict):
                            continue
                        url = safe_url(r.get("url"))
                        if not url:
                            continue
                        key = norm_url(url)
                        try:
                            score = float(r.get("score") or 0.0)
                        except (TypeError, ValueError):
                            score = 0.0
                        if key not in found or score > float(found[key].get("score") or 0.0):
                            found[key] = {
                                "title": str(r.get("title") or "")[:300],
                                "url": url,
                                "url_key": key,
                                "snippet": str(r.get("snippet") or "")[:600],
                                "score": max(0.0, score),
                                "source": q["source"],
                                "query_position": idx + 1,
                            }
            except FuturesTimeoutError:
                timed_out = sum(1 for fut in futs if not fut.done())
                failures += timed_out
                for fut, (idx, _q) in futs.items():
                    if not fut.done():
                        fut.cancel()
                        outcomes[idx]["outcome"] = "failed"
                        outcomes[idx]["error_message"] = "search timed out"
                self.emit("warn", {"message": f"{timed_out} searches were skipped because the time limit was reached."})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        ranked = sorted(found.values(), key=lambda r: float(r.get("score") or 0.0), reverse=True)[:60]
        for i, r in enumerate(ranked, start=1):
            r["id"] = f"c{i}"
        self._last_query_outcomes = outcomes
        self._last_candidates = ranked
        return ranked, failures

    # -- stage 3
    def triage(self, plan: dict[str, Any], candidates: list[dict[str, Any]], *, deadline: float | None = None) -> list[dict[str, Any]]:
        if not candidates:
            return []
        payload = {
            "summary": plan["summary"],
            "features": [f"{f['name']}: {f['description']}" for f in plan["features"]],
            "limit": self.s.max_candidates,
            "candidates": [
                {
                    "id": c["id"],
                    "title": str(c.get("title") or "")[:150],
                    "url": c["url"],
                    "source": c["source"],
                    "snippet": str(c.get("snippet") or "")[:300],
                }
                for c in candidates[:40]
            ],
        }
        try:
            out = self._call_llm("triage", prompts.TRIAGE, payload, max_tokens=1500, deadline=deadline)
        except Exception as e:
            self.emit("warn", {"message": f"Couldn't shortlist results ({type(e).__name__}); using top search results."})
            return [
                {**c, "name": _clamp_text(c.get("title") or c["url"], 200), "why": "top search result"}
                for c in candidates[: self.s.max_candidates]
            ]

        by_id = {c["id"]: c for c in candidates}
        raw_picked = out.get("picked")
        # Distinguish an intentional empty list (model rejected all listicles/unrelated pages) from invalid output.
        if isinstance(raw_picked, list) and len(raw_picked) == 0:
            return []

        picked: list[dict[str, Any]] = []
        seen: set[str] = set()
        for p in raw_picked if isinstance(raw_picked, list) else []:
            if not isinstance(p, dict):
                continue
            cid = _hashable_key(p.get("id"))
            c = by_id.get(cid) if cid else None
            if c and c["id"] not in seen:
                seen.add(c["id"])
                name = _clamp_text(p.get("name") or c.get("title") or c["url"], 200)
                picked.append({**c, "name": name, "why": str(p.get("why") or "").strip()})

        if not picked:  # malformed or unrecognized IDs: fall back to top results by search score
            picked = [
                {**c, "name": _clamp_text(c.get("title") or c["url"], 200), "why": "top search result"}
                for c in candidates[: self.s.max_candidates]
            ]
        return picked[: self.s.max_candidates]

    # -- stage 4
    def fetch_pages(self, picked: list[dict[str, Any]], *, deadline: float | None = None) -> None:
        if not picked:
            return
        try:
            pages = self._call_extract([p["url"] for p in picked], deadline=deadline)
        except Exception as e:
            self.emit("warn", {"message": f"Page extraction failed, using search snippets instead: {e}"})
            pages = {}
        by_norm = {norm_url(u): t for u, t in pages.items() if t}
        for p in picked:
            text = pages.get(p["url"]) or by_norm.get(norm_url(p["url"]))
            p["page_source"] = "page" if text else "snippet"
            raw_page = text or f"{p.get('title', '')}\n{p.get('snippet', '')}"
            p["page"] = strip_invisible(raw_page)[: self.s.page_chars]
            p["injection_flags"] = injection_flags(p["page"])
            if p["injection_flags"]:
                self.emit("warn", {
                    "message": f"{p['name']}: page contains instruction-like text "
                    f"({p['injection_flags'][0]!r}); treated as data only"
                })

    # -- stage 5
    def compare_one(self, plan: dict[str, Any], proj: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        boundary = secrets.token_hex(6)
        fenced_page = f"<<<PAGE {boundary}>>>\n{proj['page']}\n<<<END {boundary}>>>"
        payload = {"features": plan["features"], "url": proj["url"], "page": fenced_page}
        out = self._call_llm("compare", prompts.COMPARE, payload, max_tokens=1800, deadline=deadline)
        raw: dict[str, dict[str, Any]] = {}
        for f in out.get("features") or []:
            if isinstance(f, dict):
                fid = _hashable_key(f.get("id"))
                if fid:
                    raw[fid] = f

        cells: dict[str, dict[str, Any]] = {}
        bad_scores = 0
        for f in plan["features"]:
            r = raw.get(f["id"], {})
            raw_score = r.get("score", 0)
            score = parse_score(raw_score, hi=2)
            if score is None:
                bad_scores += 1
                score = 0
            quote = _clamp_text(strip_invisible(str(r.get("quote") or "")), MAX_QUOTE_CHARS)
            cells[f["id"]] = {
                "claimed": score,
                "score": score,
                "quote": quote,
                "status": "claimed" if score else "none",
                "reason": "",
            }
        if bad_scores:
            self.emit("warn", {"message": f"{proj['name']}: {bad_scores} unparseable feature score(s) defaulted to 0."})

        raw_kind = str(out.get("kind") or "other").strip().lower()
        kind = raw_kind if raw_kind in _VALID_KINDS else "other"
        return {
            "name": _clamp_text(out.get("project_name") or proj["name"], 200),
            "one_liner": _clamp_text(out.get("one_liner") or "", 300),
            "kind": kind,
            "cells": cells,
        }

    # -- stage 6
    def verify_one(self, plan: dict[str, Any], proj: dict[str, Any], *, deadline: float | None = None) -> dict[str, int]:
        feats = {f["id"]: f for f in plan["features"]}
        stats = {"checked": 0, "cut_quote": 0, "cut_verifier": 0, "downgraded": 0, "unchecked": 0}
        claims: list[dict[str, Any]] = []
        for fid, cell in proj["cells"].items():
            if cell["claimed"] == 0:
                continue
            stats["checked"] += 1
            # Check A: is the quote actually on the page? (code, not a model)
            if not quote_in_page(cell["quote"], proj["page"]):
                cell.update(score=0, status="cut_quote", reason="Quote not found on the page")
                stats["cut_quote"] += 1
                continue
            claims.append({
                "key": fid,
                "feature": f"{feats[fid]['name']}: {feats[fid]['description']}",
                "claimed_score": cell["claimed"],
                "quote": cell["quote"][:MAX_QUOTE_CHARS],
            })
        if claims:
            # Check B: does the quote support the claim? (a different model family)
            out = self._call_llm(
                "verify",
                prompts.VERIFY,
                {"project": proj["name"], "claims": claims},
                role="verifier",
                max_tokens=1500,
                deadline=deadline,
            )
            verdicts: dict[str, dict[str, Any]] = {}
            for r in out.get("results") or []:
                if isinstance(r, dict):
                    k = _hashable_key(r.get("key"))
                    if k:
                        verdicts[k] = r
            for c in claims:
                cell = proj["cells"][c["key"]]
                v = verdicts.get(c["key"])
                supported = parse_bool(v.get("supported")) if v is not None else None
                if v is None or supported is None:
                    cell.update(status="unchecked", reason="The verifier didn't return a verdict for this claim")
                    stats["unchecked"] += 1
                    continue
                if supported:
                    cell.update(status="verified", reason=str(v.get("reason") or "").strip())
                    continue
                adj = parse_score(v.get("adjusted_score"), hi=cell["claimed"] - 1)
                if adj is None:
                    adj = 0
                reason = str(v.get("reason") or "").strip()
                if adj > 0:
                    cell.update(score=adj, status="downgraded", reason=reason or "Quote shows only partial support")
                    stats["downgraded"] += 1
                else:
                    cell.update(score=0, status="cut_verifier", reason=reason or "Quote doesn't prove the claim")
                    stats["cut_verifier"] += 1
        return stats

    # -- stage 7
    def pivots(self, plan: dict[str, Any], coverage: dict[str, int], projects: list[dict[str, Any]],
               *, deadline: float | None = None) -> dict[str, Any]:
        payload = {
            "summary": plan["summary"],
            "features": [{**f, "coverage": coverage[f["id"]]} for f in plan["features"]],
            "closest": [
                {"n": p["n"], "name": p["name"], "one_liner": p["one_liner"], "url": p["url"]}
                for p in projects[:5]
            ],
        }
        out = self._call_llm("pivots", prompts.PIVOTS, payload, max_tokens=1200, deadline=deadline)
        pivots: list[dict[str, str]] = []
        for p in out.get("pivots") or []:
            if len(pivots) >= 3:
                break
            if isinstance(p, dict) and str(p.get("title") or "").strip():
                pivots.append({
                    "title": _clamp_text(p["title"], 120),
                    "pitch": str(p.get("pitch") or "").strip(),
                    "why_open": str(p.get("why_open") or "").strip(),
                })
        return {"verdict": str(out.get("verdict") or "").strip(), "pivots": pivots}

    # -- everything
    def run(self, idea: str, *, report_id: str | None = None, save_file: bool = True) -> dict[str, Any]:
        t0 = time.time()
        deadline = time.monotonic() + float(getattr(self.s, "check_deadline_s", 240))
        idea = clean_idea(idea)
        rid = report_id or new_report_id()
        log_event(logger, logging.INFO, "check.started", check_id=rid)

        self.emit("stage", {"stage": "decompose", "message": "Breaking the idea into features"})
        plan = self.decompose(idea, deadline=deadline)
        self.emit("plan", {
            "summary": plan["summary"],
            "target_user": plan["target_user"],
            "features": plan["features"],
            "queries": plan["queries"],
        })

        self.emit("stage", {"stage": "search", "message": f"Searching {len(plan['queries'])} queries across 6 source types"})
        candidates, failed = self.run_searches(plan["queries"], deadline=deadline)

        def stop(status: str, message: str) -> dict[str, Any]:
            """End early with an honest 'can't tell' instead of a made-up score."""
            self.emit("warn", {"message": message})
            return self._finish(
                t0, rid, idea, plan, [], {"verdict": "", "pivots": []},
                len(candidates), failed, status, message, save_file=save_file,
            )

        if failed == len(plan["queries"]):
            return stop(
                "failed",
                f"All {failed} searches failed (Tavily unreachable?). "
                "No novelty score is given, because there is no evidence to base it on.",
            )
        if not candidates:
            return stop(
                "insufficient_evidence",
                "The searches returned no results at all. Not enough evidence "
                "to judge whether this exists. Try describing the idea differently.",
            )

        self.emit("stage", {"stage": "triage", "message": f"Screening {len(candidates)} results"})
        picked = self.triage(plan, candidates, deadline=deadline)
        self.emit("triage", {
            "picked": [{"name": p["name"], "url": p["url"], "source": p["source"], "why": p["why"]} for p in picked]
        })

        projects: list[dict[str, Any]] = []
        verify_errors: list[str] = []
        budget_stopped = False

        if picked:
            self.emit("stage", {"stage": "extract", "message": f"Reading {len(picked)} project pages"})
            self.fetch_pages(picked, deadline=deadline)

            self.emit("stage", {"stage": "compare", "message": "Comparing and verifying each project feature by feature"})
            verify_stage_emitted = False

            def compare_and_verify(p: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int] | None, str | None]:
                cmp_res = self.compare_one(plan, p, deadline=deadline)
                proj = {
                    "url": p["url"],
                    "url_key": p.get("url_key") or norm_url(p["url"]),
                    "source": p["source"],
                    "source_label": SOURCES[p["source"]]["label"],
                    "page_source": p["page_source"],
                    "page": p["page"],
                    "injection_flags": p.get("injection_flags", []),
                    **cmp_res,
                }
                self.emit("compared", {
                    "name": proj["name"],
                    "claims": sum(1 for c in proj["cells"].values() if c["claimed"]),
                })
                try:
                    st = self.verify_one(plan, proj, deadline=deadline)
                    return proj, st, None
                except Exception as exc:
                    return proj, None, str(exc)

            max_workers = min(max(1, len(picked)), getattr(self.s, "model_concurrency", self.s.workers))
            pool = ThreadPoolExecutor(max_workers=max_workers)
            try:
                futs = {pool.submit(propagate_context(compare_and_verify), p): p for p in picked}
                timeout = max(1.0, deadline - time.monotonic())
                try:
                    for fut in as_completed(futs, timeout=timeout):
                        p = futs[fut]
                        try:
                            proj, st, v_err = fut.result()
                        except (BudgetExceeded, ProviderUnavailable) as e:
                            budget_stopped = True
                            self.emit("warn", {"message": f"Stopped early on {p['name']}: {e}"})
                            continue
                        except Exception as e:
                            self.emit("warn", {"message": f"Couldn't compare {p['name']}: {e}"})
                            continue
                        projects.append(proj)
                        if not verify_stage_emitted:
                            verify_stage_emitted = True
                            self.emit("stage", {"stage": "verify", "message": "Checking every claim against its source"})
                        if v_err is not None:
                            verify_errors.append(v_err)
                            self.emit("warn", {
                                "message": f"Verifier failed on {proj['name']}; its claims passed only the quote check: {v_err}"
                            })
                        elif st is not None:
                            self.emit("verified", {"name": proj["name"], **st})
                except FuturesTimeoutError:
                    skipped = sum(1 for fut in futs if not fut.done())
                    for fut in futs:
                        if not fut.done():
                            fut.cancel()
                    self.emit("warn", {
                        "message": f"{skipped} project(s) were skipped because the check reached its time limit."
                    })
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

            if not verify_stage_emitted and projects:
                self.emit("stage", {"stage": "verify", "message": "Checking every claim against its source"})

        if picked and not projects:
            return stop("failed", "Every comparison failed (Token Factory unreachable?). No novelty score is given.")

        for p in projects:
            p["total"] = sum(c["score"] for c in p["cells"].values())
        projects.sort(key=lambda p: (-p["total"], p["name"].lower()))
        for i, p in enumerate(projects, start=1):
            p["id"] = f"P{i}"
            p["n"] = i  # citation number
            p.pop("page", None)  # keep saved reports compact

        coverage = {f["id"]: max([p["cells"][f["id"]]["score"] for p in projects] or [0]) for f in plan["features"]}

        status, message = "ok", ""
        if not projects:
            status = "no_close_match"
            message = (
                f"Screened {len(candidates)} results and none were a close match. That suggests the idea is "
                "open, but it is a weaker signal than a full grid: absence of evidence, not proof."
            )
        elif not any(c["score"] for p in projects for c in p["cells"].values()):
            status = "no_close_match"
            message = "Related projects were found, but none of their overlap claims survived the checks."

        thin = sum(1 for p in projects if p["page_source"] == "snippet")
        if thin:
            message = (message + " " if message else "") + (
                f"{thin} of {len(projects)} project pages couldn't be read, so those were compared against search "
                "snippets only. Expect fewer confirmed overlaps."
            )
        if verify_errors:
            message = (message + " " if message else "") + (
                f"The verifier model failed on {len(verify_errors)} of {len(projects)} projects ({verify_errors[0][:120]}), "
                "so those overlap claims passed only the code quote check."
            )
        if failed:
            message = (message + " " if message else "") + (
                f"{failed} of {len(plan['queries'])} searches failed, so coverage may be incomplete."
            )
        if budget_stopped:
            message = (message + " " if message else "") + "Stopped comparing remaining projects to stay within the cost limit."

        self.emit("stage", {"stage": "pivots", "message": "Finding the open space"})
        try:
            advice = self.pivots(plan, coverage, projects, deadline=deadline)
        except Exception as e:
            self.emit("warn", {"message": f"Pivot suggestions failed: {e}"})
            advice = {"verdict": "", "pivots": []}

        return self._finish(
            t0, rid, idea, plan, projects, advice, len(candidates), failed, status, message, save_file=save_file
        )

    def _finish(
        self,
        t0: float,
        report_id: str,
        idea: str,
        plan: dict[str, Any],
        projects: list[dict[str, Any]],
        advice: dict[str, Any],
        n_candidates: int,
        failed: int,
        status: str,
        message: str,
        *,
        save_file: bool = True,
    ) -> dict[str, Any]:
        coverage = {f["id"]: max([p["cells"][f["id"]]["score"] for p in projects] or [0]) for f in plan["features"]}
        crowd = {f["id"]: sum(1 for p in projects if p["cells"][f["id"]]["score"] >= 1) for f in plan["features"]}
        totals = compute_check_totals(projects)
        result: dict[str, Any] = {
            "schema_version": 1,
            "id": report_id,
            "status": status,  # ok | no_close_match | insufficient_evidence | failed
            "message": message,
            "idea": idea,
            "summary": plan["summary"],
            "target_user": plan["target_user"],
            "features": plan["features"],
            "queries": plan["queries"],
            "query_outcomes": list(self._last_query_outcomes),
            "projects": projects,
            "sources": [
                {"n": p["n"], "name": p["name"], "url": p["url"], "source": p["source_label"]} for p in projects
            ],
            "coverage": coverage,
            "crowd": crowd,
            "gaps": [f for f in plan["features"] if coverage[f["id"]] == 0] if status in ("ok", "no_close_match") else [],
            "novelty": novelty_score(coverage) if status in ("ok", "no_close_match") else None,
            "checks": totals,
            "verdict": advice["verdict"],
            "pivots": advice["pivots"],
            "candidates_found": n_candidates,
            "searches_failed": failed,
            "models": {"primary": self.llm.model_for("primary"), "verifier": self.llm.model_for("verifier")},
            "demo": bool(getattr(self.s, "fake", False)),
            "simulated": getattr(self.s, "simulate", "") or None,
            "usage": self.budget.summary() if self.budget is not None else None,
            "seconds": round(time.time() - t0, 1),
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }

        if save_file:
            try:
                saved_path = self.save(result)
                result["saved_to"] = saved_path
            except OSError as exc:
                log_event(logger, logging.ERROR, "report.save_failed", check_id=report_id, error=type(exc).__name__)
                self.emit("warn", {"message": "This report couldn't be saved to disk. Download it now."})
                result["saved_to"] = ""

        log_event(
            logger,
            logging.INFO,
            "check.completed",
            check_id=report_id,
            status=status,
            novelty=result["novelty"],
            projects=len(projects),
            seconds=result["seconds"],
            checks=totals,
        )
        # Emit a client-safe copy without server-local filesystem paths.
        public_result = {k: v for k, v in result.items() if k != "saved_to"}
        self.emit("done", public_result)
        return result

    def save(self, result: dict[str, Any]) -> str:
        """Collision-free atomic JSON write (temp file + fsync + os.replace). Never stores absolute path in JSON."""
        d = Path(self.s.runs_dir)
        d.mkdir(parents=True, exist_ok=True)
        fname = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{secrets.token_hex(4)}.json"
        path = d / fname
        tmp_path = d / f".{fname}.tmp"
        result["file"] = fname
        payload = {k: v for k, v in result.items() if k != "saved_to"}
        data = json.dumps(payload, indent=2, ensure_ascii=False)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        return str(path)
