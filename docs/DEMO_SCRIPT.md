# Demo script (2:45)

The template asks the demo to show **a real question, the answer, opened citations, and a failure case**.
Timestamps match the judging criteria map in the README.

**Before recording**
- Run `python src/agent.py serve` with your real keys.
- Run both ideas once ("Build a research agent." and the "Prior Art itself" chip) so they're cached. Replays then come back in seconds and nothing can stall on camera.
- Pick the cell you'll open at 1:00, and check that its source page loads in your browser.

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | The empty page | "Before every hackathon or startup, the first research question is: has someone built this already? Answering it by hand takes an hour, and research agents make up overlaps." |
| 0:15–0:50 | Click **"Build a research agent"** → **Check it**. Point at the live log. | "That's this hackathon's own prompt. The writer model on Nebius Token Factory breaks it into features and plans 12 searches across six source types on Tavily. Then it reads the full pages, compares each project feature by feature, and checks every claim." |
| 0:50–1:30 | Novelty score and verdict (hover a [n] citation) → the grid → click a red cell → the drawer shows the quote → **click the source link** and find the quote on the live page. | "Every 'already done' cell is a quote from the project's own page, with its source. Here it is on the real page." |
| 1:30–1:55 | Scroll to **Claims the checks threw out** → open one. | "The writer claimed this overlap. Either the quote wasn't on the page, which plain code catches, or a second model from a different family said the quote doesn't prove it. A normal research agent would have shown you this. Ours threw it out, and shows why." |
| 1:55–2:20 | Pick **Failure demo: Tavily search down** → **Check it**. | "Simulated outage. Every search fails, so there's no score. It says it can't tell instead of guessing. If pages can't be read it falls back to snippets and says so, and a verifier outage is reported too." |
| 2:20–2:45 | **Past runs** → the "Prior Art itself" run → the green OPEN column and the pivots. | "And here's Prior Art run on itself: what's taken, what's open, and three pivots into the gap. Nebius Token Factory for both models, Tavily search and extract, and every claim checked against its source." |

**Tips**
- Keep the cursor still while the log runs. The live progress is the "it's really working" proof.
- If a live run surprises you (a weird verdict, a slow source), use the cached run. Cached runs are still real results.
- Under 3:00 total. Trim the 0:15–0:50 narration first if you run long.
