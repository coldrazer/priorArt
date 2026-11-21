# Prior Art — Research Agent

> ODSC × Nebius Hackathon submission · Challenge: *Build a research agent that brings its sources*

**One-line pitch:** Prior Art answers "has someone already built this?" for builders and founders. It returns a feature-by-feature overlap grid where every "already done" cell opens the quote and source behind it, and every quote was checked twice before it got there.

- **Demo video (2–3 min):** TODO: add link (`python scripts/submission.py fill --video <url>`)
- **Team:** Lennie Guerrero ([@coldrazer](https://github.com/coldrazer))

## The problem
Every builder, founder and hackathon team starts with the same research task: has someone built this already, and if so, where is the room left? Answering it means an hour of searching GitHub, Product Hunt, Hacker News, arXiv and Devpost by hand, and you usually end up with a vague "kind of". General research agents make it worse. They will confidently say a project "already does X" when its page says nothing of the sort.

Prior Art turns that hour into a few minutes and makes every overlap claim checkable. You get:

- the closest existing projects;
- a grid of which of your features each one already covers;
- a novelty score;
- the features nobody covers yet;
- three pivots aimed at those gaps.

Every cell links to the exact quote and source that justify it, and claims that failed the checks stay visible, crossed out, with the reason.

## How it works
```
question → features + query plan → Tavily search (6 source types) → screen & dedupe → Tavily extract (full pages)
        → compare feature by feature, with quotes (writer model) → is the quote on the page? (plain code)
        → does the quote prove the claim? (verifier model, different family) → grid · novelty · gaps · pivots, cited [n]
```
- **Query planning:** The writer model breaks the idea into 4–7 checkable features. It then writes 2 queries per source type (GitHub, Hacker News, Product Hunt, arXiv, Devpost, open web), each phrased the way that source describes things. Tavily domain filters keep each query on its source. The budget is fixed (about 12 searches, never more than 14, and at most 8 projects compared), so the agent does one wide round and stops rather than wandering.
- **Evidence handling:**
  - Results are deduplicated by normalized URL. The writer screens out listicles, news and tutorials, and merges a project found on two sites.
  - Full pages are read with Tavily Extract. If a page can't be read, the agent falls back to the search snippet and says so.
  - Every "this project already does X" must quote the page. Then two checks run: (1) plain code confirms the quote really appears on the page, so invented quotes are cut; (2) a verifier from a different model family reads only the quote and the claim, and cuts or downgrades anything the quote doesn't prove.
  - The novelty score is computed in code from checked coverage only.
- **Safety:**
  - Web content (titles, snippets, page text, quotes) reaches a model only inside the JSON payload, never in the system prompt.
  - Every prompt that sees web content marks it as untrusted data whose instructions must not be followed.
  - Pages containing instruction-like text are flagged in the UI. A planted instruction can't add a claim on its own: the claim still needs a real quote and the verifier's agreement.
  - The agent is read-only. Its only tools are search and page extraction.
- **Trade-offs:**
  - **Cost:** one run is about 12 Tavily advanced searches (2 credits each), page extraction (2 credits per 5 pages), and up to about 20 model calls, run in parallel. The verifier adds cost and time, and it is the point.
  - **Repeat runs:** calls are cached in SQLite, so re-running an idea is instant and free.
  - **Coverage:** fixed budgets trade completeness for predictable latency. A project none of the queries surface will be missed, which is why a run with no close matches is labeled "low evidence", never "novel".

## Required services
<!-- AUTO:services -->
| Requirement | How we use it |
|---|---|
| Nebius Token Factory (inference) | Two models on the OpenAI-compatible API. **Writer** `TODO: writer model`: breaks the idea into features, plans the queries, screens results, compares each project with quotes, suggests pivots. **Verifier** `TODO: verifier model` (a different model family): checks every overlap claim against its quote. |
| Tavily (real-time web search) | `search` (advanced depth, domain-filtered per source type: GitHub, Hacker News, Product Hunt, arXiv, Devpost, open web) finds candidates; `extract` reads the full pages. Every claim keeps its URL and comes out as a numbered [n] source in the grid, the verdict, the pivots and the Markdown report. Quotes are checked against the extracted page text. |
<!-- /AUTO:services -->

## Setup
<!-- AUTO:setup -->
```bash
git clone https://github.com/coldrazer/priorArt
cd priorArt
python -m venv .venv && source .venv/bin/activate     # Python 3.11+
pip install --require-hashes -r requirements.txt
cp .env.example .env   # add your keys — never commit them
python src/agent.py config check                      # validates settings (secrets masked)
python src/agent.py models                            # confirms your two models are available
python src/agent.py "Your research question here"     # e.g. "Has anyone built a scam-call detector for aging parents?"
python src/agent.py serve                             # web UI at http://127.0.0.1:8000
```
<!-- /AUTO:setup -->

- Add `--md report.md` to any run to also get a Markdown report with numbered citations.
- No keys yet? `PRIORART_FAKE=1 python src/agent.py serve` runs on built-in demo data (made-up projects, labeled "offline demo data").
- `python -m pytest -q` runs the offline test suite (no keys needed).

## Database, persistence & seeded data
The web server persists every report, progress event, rate-limit bucket, and provider usage event in SQLite (`data/priorart.sqlite`, schema `0005`, auto-migrated at startup) with anonymous owner cookies (`__Host-pa_owner` over HTTPS / `pa_owner` on local HTTP) so each browser only sees its own reports. CLI runs also write collision-free atomic JSON reports to `runs/`.

```bash
python scripts/db.py up       # apply migrations to data/priorart.sqlite
python scripts/seed.py        # load the seeded owners; safe to re-run, same result every time
python scripts/usage.py       # print today's token/credit spend against daily budget
```

Each run replaces only the seeded owners' data and prints a fingerprint; two runs print the same one. It refuses to run when `PRIORART_ENV=production`, or before migrations are applied.

There are no logins, passwords or roles in Prior Art. An owner is an anonymous browser token, and the database stores only its SHA-256 hash. The tokens below are local test values, never real secrets.

| Owner | Id | Token | What it's for |
|---|---|---|---|
| **new** | 900001 | `priorart-seed-new` | No reports: every first-use and empty state |
| **typical** | 900002 | `priorart-seed-typical` | Six reports, one per status: <ul><li>Complete, with verified, downgraded and both kinds of rejected claims (`SdTypOk001`);</li><li>Low evidence (`SdTypNcm01`);</li><li>Not enough evidence (`SdTypIns01`);</li><li>Check failed (`SdTypFail1`);</li><li>a pivot of the first report (`SdTypPvt01`);</li><li>a check still running, with a partial progress log (`SdTypRun01`).</li></ul> |
| **heavy** | 900003 | `priorart-seed-heavy` | 120 reports of history, plus `SdHvyCaps1` at every cap: a 4,000-character idea, 7 features, 14 searches, 8 projects, 3 pivots. It also has: <ul><li>very long names and verdicts;</li><li>Japanese, Arabic (right to left), emoji and accents;</li><li>one page flagged for planted instructions;</li><li>one project read from its snippet only;</li><li>one claim left unverified.</li></ul> |

All seeded content is fictional, and links use the reserved `example.com`, `example.org` and `example.net` domains.

## Example
<!-- AUTO:example -->
**Question:** Build a research agent. *(this hackathon's own prompt)*

**Answer (excerpt):** TODO: run `python src/agent.py "Build a research agent."`, then `python scripts/submission.py fill`

**Sources:** TODO
<!-- /AUTO:example -->

## Failure / insufficient-evidence case
Prior Art never invents a score it has no evidence for. Every result carries a status:

| Status | When | What you get |
|---|---|---|
| `ok` | Projects found and compared | Grid, novelty score, gaps, pivots, sources |
| `no_close_match` | Results found, but no overlap claim survived the checks | Score shown, labeled **Low evidence**: absence of evidence, not proof |
| `insufficient_evidence` | The searches returned nothing | **No score**, no pivots |
| `failed` | Every search failed, or every comparison failed | **No score**, no pivots; CLI exits with code 2 |

Partial failures degrade and say so in the result:
- a source whose search failed is named;
- pages that can't be read fall back to search snippets, with a warning;
- a verifier outage is reported, and those claims are labeled "unverified" instead of passing silently.

To show it on demand, simulate a Tavily outage (labeled as simulated in the output):
```
$ python src/agent.py "Build a research agent." --simulate search_down
...
==============================================================================
NO SCORE (failed)   ·   <the idea, restated>
==============================================================================
⚠ All 12 searches failed (Tavily unreachable?). No novelty score is given, because there is no evidence to base it on.
```
In the web UI, choose **Failure demo: Tavily search down** next to the Check it button. **Failure demo: page reading down** shows the snippet fallback.

## Judging criteria map
| Criterion | Weight | Where to look |
|---|---|---|
| Sourced and verifiable answers | 30% | Example above; every grid cell opens its quote and source; numbered [n] sources; "Claims the checks threw out" panel; demo 0:50–1:55 |
| Works end to end | 25% | Setup; `python -m pytest -q`; failure case; demo 0:15–0:50 and 1:55–2:20 |
| Usefulness | 20% | The problem; gaps and pivots; demo 0:00–0:15 and 2:20–2:45 |
| Agent design | 15% | How it works; [`src/priorart/pipeline.py`](src/priorart/pipeline.py); the two-stage claim check |
| Use of Token Factory and Tavily | 10% | Required services |

Demo walkthrough: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

## License
MIT
