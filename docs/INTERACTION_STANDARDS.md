# Prior Art — Interaction & Microcopy Standards

Phase 09 · Prompt 09.5 · Status: **proposed**. The rules every interaction follows, the words it uses, and today's violations.

## 0. Basis and conflicts

- **No glossary yet.** `docs/GLOSSARY.md` doesn't exist, and the 01.3 acceptance criteria aren't in the repo either. Until a glossary is written, the object model in `docs/UX_ARCHITECTURE.md` §1 is the glossary, and §5 below restates it with the few labels it didn't settle.
- **Binding:** the routes and labels of 09.1, the unhappy paths of 09.2 (`docs/USER_JOURNEYS.md`), the five states of 09.3 (`docs/STATE_MATRIX.md`), and the deletion policy of 03.5 (`docs/DATA_LIFECYCLE.md`). All of them are still proposed. Nothing here conflicts with them.
- **Two labels 09.1 left open**, settled in §5: the user-facing words for a downgraded claim (**Lowered**) and for a claim the verifier never confirmed (**Unverified**).
- **Audience.** "Everyone" includes people who don't know what a language model, Tavily or a verifier is. User-facing copy never needs those words. The command-line tool (`src/agent.py`) is for developers and judges, so it may name the services, but it follows the same error rules.

## 1. Forms

The one form that matters is the idea box on Home. The same rules apply to any future form, such as the "Delete my data" confirmation.

| Rule | Standard |
|---|---|
| **When validation fires** | Not while typing: no error for an unfinished sentence. On submit, and on blur only if the field was submitted once already. The character count appears from 3,500 characters and turns red past 4,000. **Check idea** stays enabled, so pressing it explains what's wrong instead of doing nothing. |
| **Where errors appear** | Directly under the field it concerns, in text (never color alone), linked to the field with `aria-describedby`. Problems that aren't about the field, such as "couldn't start", appear in the same spot with **Try again**. Never in a toast or an alert box. |
| **Input is preserved** | The idea text is never cleared by an error, a failed start, a reload of `/?idea=…`, or going back from a report. **Sample chips never overwrite typed text without a way back:** if the box has text, a sample replaces it with an **Undo** toast. |
| **Double-submit lock** | On submit the button becomes **Checking…**, disabled, with `aria-busy="true"`, until the report URL exists. Then the page moves to it. A second submit of the same text within that window is ignored. The server also refuses a second start of an identical idea from the same owner that's still running, and returns the existing report instead. |
| **Limits are stated before they bite** | The hint under the box says "A sentence or a paragraph: who it's for and what it does." The 4,000-character limit shows with the counter, not only in an error. |

## 2. Which kind of feedback

| Situation | Feedback | Examples |
|---|---|---|
| A problem with what the user typed or the region they're looking at | **Inline message**, in place | Idea too short; "Couldn't load this report."; a pivots-only failure (`docs/STATE_MATRIX.md` row H) |
| The state of the whole report | **Status banner** at the top of the report, `role="status"` (or `role="alert"` for **Check failed**) | Low evidence, Not enough evidence, Check failed, partial failures |
| The user's own action succeeded, and it can be undone or isn't visible otherwise | **Toast**, bottom of the screen, 8 seconds, paused while hovered or focused. It never steals focus and is announced politely. | "Report deleted." **Undo** · "Link copied." · "Idea replaced with a sample." **Undo** |
| An action that can't be undone and loses the user's data | **Dialog** (modal), only then | "Delete my data" |
| The result is visible where the user is looking | **Nothing** | Opening the evidence panel, changing a filter, expanding a section, a check finishing while the user watches it |
| Long work in progress | **Progress in place**, never a toast | Stage chips, live log, tab title |

**Never:**
- a toast for an error;
- a dialog for something that can be undone;
- a success message for something already visible;
- browser `alert()`/`confirm()`.

## 3. Destructive actions: proportional, undo before confirm

| Action | Consequence | Guard |
|---|---|---|
| **Delete report** | Hidden at once, restorable for 30 days (`docs/DATA_LIFECYCLE.md` §1) | **No confirmation.** Toast "Report deleted." with **Undo** (restores at once), plus **Recently deleted** for 30 days. A running check can't be deleted: the button is disabled with "Wait for the check to finish." |
| **Delete my data** (owner) | Permanent, immediate, every report | **Dialog.** States the count ("This permanently deletes your 12 reports and can't be undone."), offers **Download my data** first, and needs the word `delete` typed before the red **Delete everything** button enables. Focus starts on the text field, not the button. |
| **Replace typed idea with a sample** | Loses typed text | **Undo** toast (§1) |
| **Cancel a running check** (09.1 proposal) | Work stops; spent searches aren't refunded | No confirmation. The report shows "Check cancelled." with **Check again**. Cached searches make re-running cheap. |
| **Try again / Check this pivot** | Starts paid work | No confirmation. **Check this pivot** only prefills Home (09.1 §4); the user still presses **Check idea**. |
| Operator scripts (`scripts/data.py delete-owner`, `scripts/backup.py restore`, `scripts/db.py reset`) | Permanent | Refuse without `--yes`, and say what would happen (already implemented) |

## 4. Voice and tone

Prior Art speaks like a careful researcher talking to a friend: **plain, specific, calm, never sure of more than the evidence**.

| Principle | Before (today) | After |
|---|---|---|
| Say what happened and what to do next | "Error: Token Factory call failed for meta-llama/… after 3 tries: Connection reset" | "The AI service didn't respond, so this check stopped. Your idea is still here. **Try again** in a minute." |
| No raw codes, model names or exception text | "Search failed (GitHub): ReadTimeout(…)" | "GitHub search didn't respond; the report uses the other five sources." |
| Don't guess in a question | "All 12 searches failed (Tavily unreachable?)." | "None of the 12 searches got an answer, so there's no evidence to score. **Try again**." |
| Say what's known, not more | "No close projects found." | "Nothing we found overlaps this idea. That's a good sign, but it isn't proof nobody has built it." |
| Active, specific verbs | "No pivots generated." | "No pivots this time: there was no evidence to aim them at." |
| One word per concept (§5) | "cut", "threw out", "cut by verifier model" | "Rejected: quote doesn't prove it" |
| Sentence case, no shouting | "OPEN", "NO SCORE" | "Open", "No score" |
| No jargon | "Screen", "triage", "verifier model", "snippet" | "Shortlist", "second check", "search preview" |
| Numbers with their meaning | "23/100" alone | "Novelty 23 / 100: most of this exists already." |
| Don't blame the user | "Invalid input" | "Describe the idea in at least a sentence: who it's for and what it does." |

**Mechanics:**
- Buttons are verbs, 1–3 words (**Check idea**, **Try again**, **Download report**).
- Messages are full sentences ending with a period. Headings and labels have none.
- "You" for the user, never "we" for the product, except in the "Nothing we found…" type of honest caveat. Never "I".
- Numbers as digits.
- Times relative ("3 hours ago"), with the exact time in a tooltip.

## 5. Terms

One term per concept. Internal names on the right must never appear in the UI.

| Use | For | Never say | Internal |
|---|---|---|---|
| **Idea** | What the user submits | prompt, query, question, input | `idea` |
| **Check** (verb and noun) · **Check idea** | Running Prior Art | run, search, analyze, scan | `run` |
| **Report** | The result of one check | run, result, past run | run JSON / `reports` |
| **Feature** | One of the 4–7 parts of an idea | capability, criterion, F1 | `features` |
| **Existing project** | Something that already exists | match, competitor, candidate | `projects` |
| **Overlap: Done / Partly / Open** | One project × feature cell | score 2/1/0, covered, gap cell | `final_score` |
| **Evidence** | The quote behind an overlap | proof, citation text | `quote` |
| **Source [n]** | Numbered link to a project's page | reference, URL, link [n] | `sources`, `citation_number` |
| **Confirmed** | Overlap whose quote is on the page and proves it | verified, passed | `verified` |
| **Lowered** | Overlap the second check reduced (Done → Partly) | downgraded | `downgraded` |
| **Rejected** · reasons: **Quote not on the page** / **Quote doesn't prove it** | Overlap thrown out by the checks | cut, threw out, removed, failed | `cut_quote` / `cut_verifier` |
| **Unverified** | Overlap that passed the quote check while the second check was unavailable | claimed, unchecked | `claimed` / `unverified` |
| **Second check** | The verifier model | verifier model, checker, judge | `verifier` |
| **Gap** (header label **Open**) | A feature no existing project covers | white space, opportunity | `gaps` |
| **Pivot** · **Check this pivot** | A suggested variation | suggestion, alternative | `pivots` |
| **Novelty score** · **No score** | 0–100 from confirmed overlaps | uniqueness, rating, 0 when unknown | `novelty` |
| **Complete · Low evidence · Not enough evidence · Check failed** | Report status | ok, no_close_match, insufficient evidence, run failed | `status` |
| **Recent reports** · **Recently deleted** | Lists | past runs, history, trash | — |
| **Search preview** | Text from search results when a page couldn't be read | snippet | `page_source = snippet` |
| **AI service** · **search service** | Token Factory · Tavily, in user-facing errors | Token Factory, Tavily, LLM, API | — |

## 6. Copy deck (highest-traffic strings)

### Home
| Element | Copy |
|---|---|
| Tagline | Has someone built this already? Every overlap links to the page that proves it. |
| Idea box placeholder | Describe your idea: who it's for, what it does, and the 2–3 things that make it different. |
| Hint under box | A sentence or a paragraph is enough. |
| Primary button | **Check idea** → while starting: **Checking…** |
| Samples label | Try a sample: |
| Error: too short | Describe the idea in at least a sentence: who it's for and what it does. |
| Error: too long | That's over 4,000 characters. Trim it to the first paragraph: that's usually where the features are. |
| Error: too vague (`pipeline.py:108`) | We couldn't find distinct features in this. Add what it does and for whom, then check again. |
| Error: couldn't start | Couldn't start the check. Your idea is still here. **Try again** |
| Error: service unavailable (missing or rejected keys; visitors never see key names) | Prior Art can't run checks right now. Reports you already have still open. |
| Offline | You're offline. Your idea will stay here until you're back. |
| Recent reports, empty | Your reports will appear here. They're saved in this browser only. |

### Progress (running report)
| Element | Copy |
|---|---|
| Stage chips | Features · Search · Shortlist · Read pages · Compare · Check claims · Gaps & pivots |
| Tab title | Checking: Compare (3 of 8) — Prior Art · finished: Novelty 42 — Prior Art |
| Still working (no event for 60 s) | Still working on {stage}… |
| Connection lost | Reconnecting… The check keeps running while you're away. |
| A source failed | {Source} search didn't respond; the report uses the other sources. |
| Pages unreadable | {n} pages couldn't be read, so they were compared using their search previews. |
| Cancel | **Cancel check** |

### Report
| Element | Copy |
|---|---|
| Score label | Novelty score · No score |
| Banner: Low evidence | **Low evidence.** Nothing we found overlaps this idea after the checks. That's a good sign, not proof. |
| Banner: Not enough evidence | **Not enough evidence.** The searches found nothing to compare against. Try describing the idea differently. **Edit idea** |
| Banner: Check failed (search) | **Check failed.** None of the searches got an answer, so there's nothing to score. **Try again** |
| Banner: Check failed (AI) | **Check failed.** The AI service didn't respond, so projects couldn't be compared. **Try again** |
| Banner: second check unavailable | The second check was unavailable for {n} projects. Their overlaps are marked **Unverified**. |
| Check counts | {n} overlaps checked · {n} rejected: quote not on the page · {n} rejected: quote doesn't prove it · {n} lowered |
| Grid legend | Done · Partly · Open · Rejected |
| Grid hint | Select any cell to see its evidence. |
| Grid, no projects | Nothing we found overlaps this idea. That's a good sign, but it isn't proof nobody has built it. **Check a variation** |
| Section headings | Overlap grid · Gaps & pivots · Rejected claims · Sources |
| Gaps line | Nobody covers: {features} |
| No gaps | Every feature is covered by at least one project. The pivots aim at the thinnest coverage. |
| No pivots (no evidence) | No pivots this time: there was no evidence to aim them at. |
| Pivots failed | Couldn't suggest pivots. **Try again** |
| Rejected claims, none | Every overlap held up. Nothing was rejected. |
| Rejected claims, nothing to check | No overlaps to check. |
| Sources, none | No sources: nothing was cited. |
| Actions | **Copy link** · **Download report** · **Edit idea** · **Delete report** |
| Copy link toast | Link copied. |
| Pivot action | **Check this pivot** |
| Not found | This report doesn't exist or was deleted. **New check** |

### Evidence panel
| Element | Copy |
|---|---|
| Confirmed | **Confirmed.** The quote is on the page and shows this. |
| Lowered | **Lowered to Partly.** The quote shows only part of this. |
| Rejected: quote not on the page | **Rejected: quote not on the page.** The page doesn't contain this quote. |
| Rejected: quote doesn't prove it | **Rejected: quote doesn't prove it.** {second-check reason} |
| Unverified | **Unverified.** The quote is on the page, but the second check was unavailable. |
| Open | **Open.** Nothing on this project's page shows this feature. |
| Search preview note | This page couldn't be read, so it was compared using its search preview. |
| Planted-instruction note | This page contains text that tries to instruct AI tools. It was treated as plain text. |
| Source link | Open source (opens in a new tab) |
| Quote date | Quoted on {date}. The page may have changed since. |

### Deleting
| Element | Copy |
|---|---|
| Delete report toast | Report deleted. **Undo** |
| Running check | Wait for the check to finish before deleting it. |
| Recently deleted, empty | Nothing deleted in the last 30 days. |
| Restore expired | This report was deleted over 30 days ago and can't be restored. |
| Delete my data: dialog title | Delete all your data? |
| Delete my data: body | This permanently deletes your {n} reports and can't be undone. Download a copy first if you want one. |
| Delete my data: field label | Type **delete** to confirm |
| Delete my data: buttons | **Download my data** · **Cancel** · **Delete everything** |
| After deletion | Your data was deleted. |

### Generic
| Element | Copy |
|---|---|
| Unexpected error | Something went wrong on our side. Nothing you entered was lost. **Try again** |
| Slow request (10 s) | Still working… |
| Request timed out (30 s) | This is taking too long. **Try again** |

## 7. Keyboard and focus

| Interaction | Expectation |
|---|---|
| **Idea form** | <ul><li>Enter adds a line; ⌘/Ctrl+Enter submits.</li><li>On a validation error, focus stays in the box and the error is announced.</li><li>On submit, focus moves to the report's heading.</li></ul> |
| **Buttons, chips, check counts, rejected-claim rows** | Real `<button>` or `<a>` elements: reachable with Tab, activated with Enter/Space, with a visible focus ring. No clickable `<div>` or `<td>`. |
| **Overlap grid** | <ul><li>One Tab stop into the grid, then arrow keys move between cells.</li><li>Enter opens the evidence panel.</li><li>Each cell has an accessible name, such as "PolicyDiff, Change detection: Done".</li></ul> |
| **Evidence panel** (non-modal side panel) | <ul><li>Opening moves focus to its heading.</li><li>Arrow keys move to the previous or next overlap.</li><li>Esc or ✕ closes it and returns focus to the cell that opened it.</li><li>Tab isn't trapped.</li></ul> |
| **Dialog** ("Delete my data") | <ul><li>Modal, and focus is trapped.</li><li>Focus starts on the confirmation field.</li><li>Esc cancels and returns focus to the trigger.</li><li>The destructive button is never the default action.</li></ul> |
| **Toast** | <ul><li>Doesn't take focus and is announced politely.</li><li>Its action (**Undo**) is reachable with Tab.</li><li>It stays while focused or hovered.</li></ul> |
| **Menus** (Recent ▾) | <ul><li>Enter/Space/↓ opens; arrow keys move; Enter picks.</li><li>Esc closes and returns focus.</li></ul> |
| **Progress** | <ul><li>The stage change is announced politely, once per stage, not per log line.</li><li>The log is `aria-live="off"`, so it doesn't flood screen readers.</li></ul> |
| **Status banners** | `role="status"`; **Check failed** uses `role="alert"` |
| **Links that leave Prior Art** | Open in a new tab with `rel="noopener"`, marked with a visible icon and "(opens in a new tab)" for screen readers |

## 8. Existing strings and behaviors that violate this standard

All in `src/priorart/static/index.html` unless noted.

| # | Where | Today | Rule broken | Fix (copy deck §6) |
|---|---|---|---|---|
| 1 | `:124` | Button "Check it" | §5 term | **Check idea** / **Checking…** |
| 2 | `:238` | Too-short idea just refocuses the box, no message | §1 errors appear in text; §2 inline | "Describe the idea in at least a sentence…" |
| 3 | `:247`, `:256–263`; `server.py:73` | Over-length idea and dropped connections end silently; the server answers a too-long idea with a raw 422 response | §1, §4 no silent failure, no raw codes | Inline length error; "Reconnecting…" |
| 4 | `:215` | Sample chips overwrite typed text with no way back | §1 input preserved; §3 undo | Undo toast |
| 5 | `:257` with `pipeline.py:137, 180, 308, 330` and `llm.py:91, 96` | Raw exception text, service and model names reach the user ("Token Factory refused the request for …: …") | §4 no raw codes or jargon; §5 | Plain messages (§6 Generic, Progress, Report); details to the server log |
| 6 | `pipeline.py:285, 317` | "(Tavily unreachable?)", "(Token Factory unreachable?)": guesses posed as questions, with vendor names | §4 | "None of the searches got an answer…" / "The AI service didn't respond…" |
| 7 | `:281`, `:286` | Banner titles "Run failed.", "Insufficient evidence.", generic "Note." | §5 statuses | **Check failed**, **Not enough evidence**, **Low evidence**; a partial-failure banner without a vague "Note." |
| 8 | `:288` | "No verdict: run failed" built by lowercasing a title | §4 | The status banner carries the meaning; the verdict line is hidden |
| 9 | `:164`, `:177`, `:297–298`, `:314`, `:337` | "cut", "Claim cut by checks", "Claims the checks threw out", "cut: quote not on page", "cut by verifier model", "Nothing was cut." | §5 **Rejected** | Copy deck: Report, Evidence panel |
| 10 | `:344–349` | Panel statuses "Verified: …", "Downgraded by the verifier", "Cut: …", "Unverified: … verifier model failed" | §5 Confirmed / Lowered / Rejected / Unverified; no "verifier" | Copy deck: Evidence panel |
| 11 | `:358`, `:360` | "Model claimed: Done → final: none"; "Checker's note" | §4 jargon | "Claimed Done, lowered to Partly" / "Why" |
| 12 | `:305`, `:321` | "OPEN" in caps in headers, "Open" in the coverage row | §4 sentence case; one form | **Open** everywhere |
| 13 | `:173` | "Where the open space is" | 09.1 §7 label | **Gaps & pivots** |
| 14 | `:132`, `:209–210` | "Past runs…"; failed runs show "null/100" | §5; §4 numbers with meaning | **Recent reports**; status label instead of a score |
| 15 | `:183` | "Download report (.md)" | §4 buttons are verbs, no file jargon | **Download report** |
| 16 | `:203–204` | Header shows "missing NEBIUS_API_KEY, TAVILY_API_KEY" to any visitor; "offline demo data" | §4, §6 service unavailable | Visitors: "Prior Art can't run checks right now"; demo mode: **Demo data** |
| 17 | `:127–130` | Public "Failure demo: Tavily search down" menu | §4 jargon; 09.1 §3 decision | Hidden unless the server runs in demo mode |
| 18 | `:197`, `:248`, `:255`; `pipeline.py:291` | Stage "Screen", log glyphs "▸" and "!", "Screening N results" | §4 jargon | **Shortlist**; plain log lines |
| 19 | `:315`, `:334`, `:297–300` | Grid cells, rejected-claim rows and check counts are clickable `<td>`/`<div>` elements: not reachable by keyboard | §7 | Buttons, and arrow-key navigation in the grid |
| 20 | `:342`, `:216–217` | Opening the evidence panel doesn't move focus; closing doesn't return it | §7 | Focus to the panel heading; back to the cell on close |
| 21 | `:268`, `:310`, `:363` | Links open new tabs with no indication | §7 | Icon plus "(opens in a new tab)" |
| 22 | `:319`, `:327` | "No close projects found." (and that message is unreachable, `docs/STATE_MATRIX.md` #10); "No pivots generated."; "Skipped: no evidence to base pivots on." | §4 say what's known; active voice | Copy deck: Report |
| 23 | `:240`, `:263` | The double-submit lock is only the disabled button, with no busy state or text change; the server would start a duplicate check for the same idea | §1 | **Checking…** with `aria-busy`; server-side duplicate guard |
| 24 | `src/priorart/__main__.py:43, 58, 61, 142` | CLI prints "NO SCORE (failed)" using the internal status name, "cut by verifier", and raw "Error: {exception}" | §4, §5 (the CLI may name services, but not raw codes or internal tokens) | "No score: check failed"; "rejected"; plain error plus a hint |
| 25 | — | No toasts, dialogs, delete, restore or copy-link exist yet | §2, §3 | Build to this standard when the routes from 09.1 land |

## Follow-ups (out of scope for this phase)

- **Write `docs/GLOSSARY.md`** from §5, the single source for terms, and make 09.1 §1 point to it.
- **Fix violations 1–24** in the page, server and pipeline messages. Small text changes (1, 7, 9–15, 18, 22) are safe before the hackathon deadline. Behavior changes (2–5, 19–21, 23) need a Go, and most depend on 09.1's routes.
- **Move raw exception detail to a server log** instead of `emit("warn")` messages, so the pipeline can keep detail for debugging without showing it.
- **Lint strings:** a test that scans `index.html` and pipeline messages for banned terms from §5 ("cut", "verifier", "Tavily", "Token Factory", "run failed"), so violations can't come back.
- **Update README and DEMO_SCRIPT** wording ("Claims the checks threw out", "Past runs", "Failure demo") once the UI copy changes.
