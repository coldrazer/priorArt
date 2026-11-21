# Prior Art — Zero Fake Code & Broken Wiring Audit (S.02)

Scanned 2026-11-20: every route in `src/priorart/server.py`, the pipeline and its clients (`pipeline.py`, `llm.py`, `search.py`, `cache.py`, `fakes.py`, `__init__.py`), the database layer (`db.py`, `queries.py`, `lifecycle.py`), and the single page (`src/priorart/static/index.html`).

What the hunt found, in short:

- **TODO/FIXME in code:** none. The only TODO markers are in `README.md` (`:7`, `:49`, `:56`, `:95`, `:97`).
- **Artificial delays:** one, `time.sleep(0.12)` in the fake clients (`fakes.py:93–94`). The other sleep is a real retry backoff (`llm.py:95`).
- **Stream events:** every event the server emits has a listener on the page, and the reverse holds too (`pipeline.py:137–401` against `index.html:248–261`). The breaks are in what happens around them: errors, reloads, persistence.
- **Persistence:** no report is ever written to the database. The 03.x schema, pool, queries and lifecycle code are complete and tested but no route uses them.

Overlap with `DAY1_PRODUCTION_PLAN.md` is marked "(S.01 #NN)"; those items aren't repeated in detail.

### #01 [P0] Claims the verifier skipped are presented as verified
- Location: `src/priorart/pipeline.py:231–235`. Same issue as S.01 #01.
- Risk: The "checked" label is faked for any claim the verifier's response leaves out, so the grid, drawer, verdict citations and Markdown export all overstate what was checked.
- Fix: An `unchecked` status end to end: set it in `verify_one`, count it in `checks.unchecked` (`pipeline.py:334` `verified` event and the totals), render it in the grid cell legend, the drawer (`index.html:342–368`) and `report.py:43`, and test with a fake verifier that drops a claim.

### #02 [P0] A "false" from the verifier is read as approval
- Location: `src/priorart/pipeline.py:236`. Same issue as S.01 #02.
- Risk: A rejected claim is shown as verified whenever the model returns `"false"` as a string.
- Fix: Strict boolean parsing shared by the verifier and compare steps; anything unparseable becomes `unchecked` (#01); tests per value form.

### #03 [P0] Finished reports are lost when two checks save in the same second
- Location: `src/priorart/pipeline.py:404–408`. Same issue as S.01 #03.
- Risk: Past runs and the download link point at a file another check overwrote; 97 of 128 reports disappeared in today's load test.
- Fix: Unique id in the file name and an atomic write (temporary file, `fsync`, `os.replace`); test with 50 saves in one second.

### #04 [P0] The README shows TODO markers instead of a real run
- Location: `README.md:7` (demo link), `:49` (writer and verifier model names), `:56` (clone URL), `:95` (example answer), `:97` (sources). Same issue as S.01 #17.
- Risk: The first thing a judge opens says "TODO" where the evidence of a working agent should be.
- Fix: One live run with real keys, then `python scripts/submission.py fill` (it rewrites the AUTO blocks and writes `docs/example-report.md`), plus `--repo` and `--video`; `submission.py check` must show no ✗.

### #05 [P1] Reports never reach the database
- Location: writes `src/priorart/pipeline.py:400–408` (JSON file only); reads `src/priorart/server.py:32–53` (folder listing); the unused layer `src/priorart/db.py:53–99`, `src/priorart/queries.py:40–46`, `:83–85`, `src/priorart/lifecycle.py:54–171`. Same issue as S.01 #04.
- Risk: Every data guarantee designed in 03.x (ownership, soft delete, restore window, audit, backups, export) applies to an empty database while real reports sit in loose files.
- Fix: A `ReportStore` with `save(result, owner_id)` writing every table in one `lifecycle.transaction`, and `get(id, owner_id)`/`list(owner_id)` through the named queries; `_finish` calls it; `/api/runs` and `/api/runs/{id}` read from it; a one-time import of `runs/*.json`. Integration test: a check run through the server is readable through `queries.report_by_id`.

### #06 [P1] A report disappears on reload and can't be linked to
- Location: `src/priorart/static/index.html:236–262` (results live only in page variables; no `history.pushState`, no report URL), `:214` (a past run opens without changing the URL); designed route `/r/{reportId}` in `docs/UX_ARCHITECTURE.md:118`.
- Risk: Refreshing, or pressing back, wipes the report and the idea text. A user can't bookmark a result or send it to a cofounder, which is the main thing people do with a report.
- Fix: Give each report a stable id (#05); serve `GET /r/{id}` (the same page) and `GET /api/reports/{id}`; on `done` or when opening a past run call `history.pushState` to `/r/{id}`; on load, if the path is `/r/{id}`, fetch and render it; keep the draft idea in `sessionStorage` until a check starts.

### #07 [P1] A running check can't be found again after the page closes
- Location: `src/priorart/server.py:85–102` (the check keeps running in its thread, but its events go only to the one open stream); `src/priorart/static/index.html:261` (Past runs refreshes only on `done`); progress-log table designed for this `docs/DATA_MODEL.md:162` (`report_events`) and unused.
- Risk: Close the tab, lose the connection or reload during the two-minute wait, and the check silently continues and spends credits, but the user sees nothing and has no way back to it until it happens to appear in Past runs.
- Fix: Create the report row with status `running` before the check starts; append every event to `report_events`; `GET /api/reports/{id}/events?after=<seq>` replays stored events and then follows live ones; the page reconnects with the last seq it saw; Past runs shows running checks with a "Running…" label.

### #08 [P1] Ideas that are too short or too long fail without a word
- Location: `src/priorart/static/index.html:238` (`if (idea.length < 10) { $("#idea").focus(); return; }`), `:122` (textarea with no `maxlength`); server limit `src/priorart/server.py:73` (`max_length=4000`) → 422, which reaches the page as a stream error with no data (`index.html:256–259`) and is dropped.
- Risk: Clicking **Check idea** does nothing visible for a short idea; a long one (common when pasting a full pitch) starts, then stops with no message.
- Fix: Validate on submit with the messages from `docs/INTERACTION_STANDARDS.md:18` (a live count from 3,500 characters, red past 4,000); start checks with a `POST` that returns validation errors as JSON (S.01 #05), shown under the field.

### #09 [P1] A dropped connection looks like a finished run
- Location: `src/priorart/static/index.html:256–259`: one handler serves both the server's `error` event and the browser's own connection error; without `e.data` it just calls `finish()`.
- Risk: The progress stops, the button re-enables and no message appears, so users assume the check is done or broken while it is still running on the server.
- Fix: Name the server's event `check_error` (and update `server.py:88`); treat the browser's `error` as "connection lost", show a banner with **Reconnect**, and reconnect with the replay endpoint from #07.

### #10 [P1] Requests that fail are rendered as if they succeeded
- Location: `src/priorart/static/index.html:201–206` (`/api/status`), `:208–212` (`/api/runs`), `:214` (`/api/runs/{name}` → `render(r)` on whatever came back); none check `r.ok` or catch.
- Risk: Opening a past run that no longer exists renders the `{"detail":"Not Found"}` body as a report and throws partway, leaving a half-drawn page; a failed status call leaves the status pill blank.
- Fix: One `api(path, opts)` helper that throws on `!r.ok` with the server's message; each caller shows an inline error with **Try again** (S.01 #13).

### #11 [P1] A finished, paid-for result is thrown away if saving fails
- Location: `src/priorart/pipeline.py:400–401` (`saved_to = self.save(result)` runs before `emit("done", result)`); a write error propagates to `src/priorart/server.py:87–88` and the page only gets an error message.
- Risk: A full disk or a permissions problem turns a two-minute, paid check into "Run failed", even though the whole result was in memory.
- Fix: Emit `done` with the result first, then save; on a save failure emit a `warn` ("This report couldn't be saved, download it now") and keep the Markdown download working from the in-memory result via a short-lived server-side copy; log the failure.

### #12 [P1] "Run it again" doesn't search again
- Location: `src/priorart/cache.py:32–45` (every result cached forever, including empty search results, with no expiry or bypass); used by `src/priorart/search.py:38–46` (search), `:56–60` (extract) and `src/priorart/llm.py:103–117`.
- Risk: Re-checking an idea next week returns last week's search results and model answers, and the report presents them as fresh. One transient empty search is cached permanently, so that idea keeps getting "insufficient evidence".
- Fix: Store a timestamp per entry; expire search and extract entries after 24 hours and model entries after 7 days; never cache empty or error results; add a "Search fresh" option that bypasses the cache for one check; show "results from <date> (cached)" in the report when cache entries were used.

### #13 [P1] Fake mode and its invented data ship in the runtime and add an artificial delay
- Location: `src/priorart/fakes.py:93–94` (`time.sleep(0.12)` before every fake call), fabricated projects and claims `fakes.py:85–91`; switched on by `PRIORART_FAKE` in `src/priorart/__init__.py:13–16`; only a small pill tells users (`index.html:203`).
- Risk: A server started with `PRIORART_FAKE=1` (left over from a demo) serves invented projects and quotes to real users as if they were search results, with a fake delay making them look like real work.
- Fix: Remove the sleep (tests don't need it); move `fakes.py` under `tests/` with a test-only import path; for an offline demo keep a clearly marked mode that refuses to run when `PRIORART_ENV=production` and shows a full-width "Demo data, not real search results" banner on every report it produced, with `"demo": true` saved in the report.

### #14 [P1] The outage simulator is a live control in the product
- Location: `src/priorart/static/index.html:127` (the selector), `:246–247` (sent as `&simulate=`); `src/priorart/server.py:73–77`; `src/priorart/__init__.py:29–30`. Same issue as S.01 #06.
- Risk: Any visitor can make checks fail on purpose, and reports produced that way look like genuine outages.
- Fix: Render the selector only when the server says `demo_controls: true` (not in production); reject `simulate` in production; mark simulated reports with `"simulated": "search_down"` and show it on the report.

### #15 [P2] Past runs shows "/100" for reports without a score
- Location: `src/priorart/static/index.html:209–210` (`${esc(r.novelty)}/100` where `novelty` is `null`); `src/priorart/server.py:41–42` (status isn't returned).
- Risk: Failed and insufficient-evidence runs appear as "/100 · …", which reads as broken, and users can't tell which past runs actually have a result.
- Fix: Return `status` from `/api/runs`; label entries "No score: searches failed", "No score: not enough evidence" or "Novelty 23/100".

### #16 [P2] Opening a past run leaves the previous run's state on screen
- Location: `src/priorart/static/index.html:214` (renders without the reset done at `:241–245`); the selector keeps its value, so choosing the same run again does nothing.
- Risk: The old progress log, error banner and stage chips stay next to a different report, which looks like that report failed.
- Fix: A single `showReport(r)` that clears the progress panel, banner and stage chips, renders, updates the URL (#06) and resets the selector.

### #17 [P2] Data the pipeline computes and saves is never shown
- Location: saved in `src/priorart/pipeline.py:382` (`target_user`), `:384` (`queries`), `:388` (`crowd`), `:395` (`searches_failed`); none are read by `index.html` `render()` (`:273–341`). The search log exists only in the live progress panel (`:250`), so it is gone for every reopened report.
- Risk: Users can't see what was searched, which sources failed, or how crowded each feature is, so a reopened report can't be audited, though the product promises traceable evidence.
- Fix: A "How this was checked" section: the queries by source with result counts, failed searches, the target user, and a per-feature "N projects do this" count from `crowd`.

### #18 [P2] The download link depends on the server's file path and is sometimes a dead link
- Location: `src/priorart/static/index.html:183` (`href="#"`), `:290` (filename taken by splitting `r.saved_to`, an absolute server path).
- Risk: When a result has no `saved_to` the link silently does nothing; once `saved_to` is removed for security (S.01 #08) the link breaks completely.
- Fix: Link to `/api/reports/{id}.md` using the report id; hide the link until a report is loaded; serve the Markdown from the database (#05).

### #19 [P2] Pivots have no action
- Location: `src/priorart/static/index.html:325–327` (pivots render as text only); designed action in `docs/USER_JOURNEYS.md:135` and `docs/UX_ARCHITECTURE.md:150`.
- Risk: The report ends at its most useful moment, the pivot, with nothing to click; users must copy the text by hand.
- Fix: A **Check this pivot** link per pivot to `/?idea=<title + pitch>` (prefill only, never auto-start, per `UX_ARCHITECTURE.md:167`); Home reads `idea` from the query string into the textarea.

### #20 [P2] Delete, restore and export exist only as operator scripts
- Location: `src/priorart/lifecycle.py:54` (`delete_report`), `:62` (`restore_report`), `:120` (`export_owner`), `:156` (`delete_owner`); exposed only through `scripts/data.py`; no route in `src/priorart/server.py:20–72`.
- Risk: A user who pastes a confidential idea has no way to delete it, which matters as soon as the app is hosted.
- Fix: Once reports have owners (#05, S.01 #07): `DELETE /api/reports/{id}` (soft delete with a 30-day **Undo**), `POST /api/reports/{id}/restore`, `GET /api/me/export`; buttons on the report and in Past runs, with copy from `docs/INTERACTION_STANDARDS.md`.

## Suggested batches

| Batch | Items | Notes |
|---|---|---|
| **A: before submission** | #01, #02, #03, #08, #10, #15 | Small, contained, visible to judges; about 1.5 hours with tests |
| **B: before submission (you)** | #04 | Live run, repo, video |
| C: the persistence spine | #05, #06, #07, #09, #11, #18 | One connected change: database store, report URLs, replayable events |
| D: honesty of results | #12, #13, #14, #17 | Cache freshness and clearly labeled demo data |
| E: completeness | #16, #19, #20 | After submission |
