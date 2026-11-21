# Prior Art — 5-State Matrix

Phase 09 · Prompt 09.3 · Status: **proposed**

## 0. Basis and conflicts

- **Earlier phases this builds on.** It uses the screens and labels from `docs/UX_ARCHITECTURE.md` (09.1) and the journeys and unhappy paths from `docs/USER_JOURNEYS.md` (09.2). Both are still **proposed**; 09.1 is awaiting approval (`docs/UX_ARCHITECTURE.md:3`).
- **Rows and naming.** Rows are the regions of today's single page (`src/priorart/static/index.html`) plus the screens 09.1 adds. Rows marked *(proposed)* don't exist yet.
- **What "Today" means.** Each row says what the code does today and what the design requires. Section 5 lists every state that is missing today.
- **No conflicts with earlier docs.** One clarification: the "running" Report from 09.1 is not a Loading state. Waiting for a check is its own long, informative state (09.2, J1 step 4), so it appears below as the **Progress** region with its own five states.

## 1. Rules that apply to every row

| Rule | Spec |
|---|---|
| **Loading delay** | Nothing appears for the first **250 ms**. If data arrives sooner, render Ideal directly. Once shown, a skeleton stays at least **400 ms** so it doesn't flash. |
| **Skeleton geometry** | Same boxes, sizes and grid as the Ideal layout: the 210 px score column (`index.html:44`), the 250 px project column (`:66`), and one column per feature. Gray blocks, no text, no spinner. They pulse unless `prefers-reduced-motion` is set. |
| **Empty** | Always says what belongs here and gives the action that creates it. **First-use** (nothing yet) and **no-results** (we looked and found nothing) never share wording. |
| **Error** | Plain language: what failed, not a stack trace or key name. Whatever the user typed is never cleared. **Retry** sits in the same region, and one region failing never blanks another. |
| **Partial** | Show what's ready, mark what isn't. A note goes in the region it affects, not only in a top banner. Never block the usable part. |
| **Ideal** | Specified for a typical and an extreme case (Section 3). All web text is escaped before display (`esc`, `index.html:196`). |
| **Slow network** | Any request still pending after **10 s** shows "Still working…" in place. Report reads time out after **30 s** and become Error with Retry. |
| **Offline** | Detected with `navigator.onLine` plus the `online`/`offline` events. A slim bar under the header reads "You're offline." Each region keeps what it already shows. |
| **Status wording** | Report status labels (09.1 §1): **Complete**, **Low evidence**, **Not enough evidence**, **Check failed**. |

## 2. The matrix

Loading · Empty · Error · Partial · Ideal, for each screen or async region.

| Region (code today) | Loading | Empty | Error | Partial | Ideal |
|---|---|---|---|---|---|
| **A. Header status indicator**: `/api/status` (`index.html:118`, `:201–205`) | Nothing: the indicator is hidden until it loads. **Today:** shows "…" indefinitely. | Live mode with keys set: show nothing. It's only for demo/offline mode (09.1 §3). | Status couldn't load: show nothing. The first check will surface any real problem. **Today:** no error handling; stays "…". | — | Demo mode: "Demo data" pill. Live mode: no pill; models are listed in the report footer. |
| **B. Home · idea form** (`index.html:122–133`) | Static: no loading state. While a check is being created: button reads **Starting…** and is disabled; the idea stays editable. | **First use:** placeholder with a shaped example ("who it's for, what it does, 2–3 key features"), plus the two sample ideas (`:125–126`). | **Validation:** message under the box (under 10 / over 4,000 chars; `server.py:73`). **Couldn't start:** "Couldn't start the check. Your idea is still here." with **Try again**. **Today:** too-short just refocuses (`:238`); too-long and network failures end silently (`:247`, `:256–263`). | — | Idea in the box, a character count (shown from 3,500 up), **Check idea** enabled |
| **C. Recent reports** (Home list and header **Recent ▾**). **Today:** "Past runs…" dropdown filled from `/api/runs` (`:132`, `:207–212`). | 3 skeleton rows (title bar, score chip, date) after the 250 ms delay | **First use:** "Your checks will appear here. They're saved in this browser only." **No results** (storage cleared): same text plus "Reports are still available at their links." **Today:** an empty dropdown is identical to a failed one. | Can't apply to the browser list, which needs no network. The server list it replaces has no error handling today (`:208`). | Some saved reports were removed on the server: show them greyed as "No longer available" with **Remove**. | Up to 10 rows: summary (one line, ellipsis), novelty or status, relative date. **Today:** novelty shows "null/100" for failed runs (`:210`). |
| **D. Opening a saved report**. **Today:** pick from "Past runs" → `/api/runs/{name}` (`:214`). *(proposed: navigating to `/r/{id}`)* | Report skeleton (F + G geometry) after 250 ms. **Today:** nothing; the old view stays until the new one replaces it. | — (a report always has content once loaded) | **Not found:** the Report not found screen (row M). **Network/server:** "Couldn't load this report." with **Retry**, in place of the report. **Today:** no error handling; nothing happens (`:214`). | — | The report renders (rows F–K) |
| **E. Progress** (Report, running): stream from `/api/run` (`:136–139`, `:242–261`) | First 250 ms: nothing. Then the 7 stage chips (`:197`) with the first one active. A stage event normally arrives immediately. | — (a running check always has a stage) | **Check couldn't start or crashed:** red banner in the progress card (exists, `:258`), with **Try again** and **Edit idea**. The idea stays in the box. **Connection lost:** "Reconnecting…", then resume from the report's saved state. **Today:** a dropped connection closes the stream silently (`:256–263`); there's no Retry. | Warnings appear inline in the log in amber (exists, `:252`): a failed search source, unreadable pages, a verifier failure. **Designed:** grid rows appear as each project is compared (row G, Partial). | Stages advance; the log shows features, searches by source, screened projects, and claims per project; the tab title shows the stage |
| **F. Report summary**: score, verdict, status banner, check counts (`:143–154`, `:273–300`) | Skeleton: 210 px score block, two text bars, four count tiles | — (always has a status). **No results:** status **Not enough evidence** shows **No score**, the reason, and **Edit idea** / **Try again**. | Status **Check failed**: red banner with the reason (exists, `:282`), **No score** (exists, `:277`), **Try again** / **Edit idea** *(proposed)*. | **Low evidence** (`no_close_match`): score shown with the amber banner explaining why (exists). The banner also covers some searches failed, pages read from snippets, or the verifier failed (exists; message built in `pipeline.py`). | Novelty number, colored bar, verdict with [n] links, "N closest projects from M results · Xs", four check counts linking to Rejected claims (exists) |
| **G. Overlap grid** (`:157–168`, `:302–321`) | Once features are known (the stream's `plan` event), draw the real header row with feature names and up to 8 skeleton rows (`config.py:42` default `max_candidates=8`). Before that, a 5-column skeleton. | **No results:** "No existing project overlapped any feature after the checks. That suggests the idea is open, but absence of evidence isn't proof." with **Check a variation**. **Today:** the whole grid card is hidden when there are no projects (`:289`), so the empty-row message at `:319` can never be seen. | Region-level: if comparisons failed (status **Check failed**), show "Comparisons couldn't be completed." with **Try again**, and keep feature headers so the idea's breakdown stays visible. | **Designed:** rows appear as each project finishes; rows still comparing show skeleton cells and "Comparing…". Snippet-only rows get a "snippet" marker. Cells whose verifier failed show **Unverified** (drawer text exists, `:342+`). | Rows sorted by overlap; cells **Done / Partly / Open** or struck **Rejected**; Open column headers marked; Best coverage row; legend |
| **H. Gaps & pivots** (`:172–174`, `:322–329`) | 3 card skeletons (title bar, two text lines) | **No results (no gaps):** "Every feature is covered by at least one project. The pivots below aim at the thinnest coverage." **First use:** n/a. | **Pivots failed** (`pipeline.py:370–371`): "Couldn't generate pivots." with **Retry pivots**, while gaps still show. **Today:** shows "No pivots generated.", the same as a real empty result (`:327`). | Gaps shown, pivots still loading or failed (as Error, in the pivots slot only) | "Nobody does: …" gap list, then 3 pivot cards with [n] links and **Check this pivot** *(proposed)* |
| **I. Rejected claims** (`:176–178`, `:330–338`) | 2 row skeletons | **No results, claims checked:** "Every claim held up. Nothing was rejected." (exists). **Nothing to check:** "No overlap claims to check." (exists, `:337`). | Verifier unavailable: "The verifier was unavailable for N projects; their claims passed only the quote check." with a link to those cells. Today this lives in the top banner only. | Some projects not yet checked: listed as "Checking…" | Each rejected or downgraded claim: project → feature, reason label, the struck quote, the checker's note; clicking opens the drawer |
| **J. Sources + Download report** (`:181–184`, `:289–292`) | 4 line skeletons | **No results:** "No sources: nothing was cited." (exists, `:292`). The download link stays, since the report still records the failure. | Download failed (row L) | Sources shown before the report is saved: the download link is disabled with "Available when the check finishes" | Numbered [n] list with name, source type and URL; **Download report** |
| **K. Evidence drawer** (`:189–191`, `:342–363`) | None needed; data is already in the page. *(proposed deep link `?project&feature`)*: the drawer opens once the report loads, showing a skeleton inside the drawer until then. | Open cell: "No evidence: this feature looks open on this project's page." (exists, wording per 09.2 J2) | Deep link to a cell that doesn't exist: drawer stays closed and the bad parameter is dropped. **Opening the source failed:** the browser's own page; the quote stays readable in the drawer. | Snippet-only evidence: note "Full page couldn't be read…" (exists, `:361`). Verifier failed: **Unverified** label (exists). | Feature, project and kind, overlap value, quote, check verdict and note, source [n] link, "Quoted on {date}" *(proposed)* |
| **L. Markdown download** `/api/report/{name}` (`server.py:61`) | Link reads **Preparing…** after 250 ms | — | "Couldn't download the report." with **Retry** next to the link. **Today:** a failed download opens the server's error page. | — | Downloads `<report>.md` (exists) |
| **M. Report not found** *(proposed, 09.1)* | — | "This report doesn't exist or was removed." with **New check** and Recent reports | — | — | — |

## 3. Ideal at typical and extreme content lengths

| Content | Typical | Extreme | Rule |
|---|---|---|---|
| Idea | 1 paragraph, ~300–600 chars | 4,000 chars (`server.py:73`) | Home: the textarea grows to 12 lines, then scrolls. Report header: the summary sentence only (`pipeline` `summary`), never the full idea. The full idea sits behind **Show full idea**. |
| Features | 5 | 7 (`pipeline.py:102`) | Grid min-width 720 px (`index.html:62`); scroll sideways inside the grid card (`:61`) with the project column sticky. Feature names wrap to 2 lines, then ellipsis; the full description is in a tooltip (`:305`). |
| Projects | 5–8 | 8 (default cap, `config.py:42`); 1 | One row: no extra spacing, the Best coverage row still shown |
| Project name / one-liner | 2–4 words / ~12 words | 80+ chars / 300+ chars | Name: 2 lines max in the 250 px column, then ellipsis with the full name in the title. One-liner: 1 line, ellipsis. |
| Quote (drawer) | 1 sentence | 600+ chars | Full quote in the drawer; it scrolls inside the drawer. Rejected-claims list: 2 lines, then ellipsis. |
| Verdict | 1 sentence | 400+ chars | 3 lines, then **More** |
| Pivots | 3 | 0–3 (`pipeline.py`, `[:3]`) | Fewer than 3: render what exists; zero: see row H |
| Sources | 5–8 | 8 URLs, each 200+ chars | URLs break anywhere (`word-break: break-all` exists) |
| Past/recent summary | ~60 chars | Long | Truncated to 60 today (`index.html:210`); keep it, with ellipsis |
| Progress log | ~30 lines | 100+ lines with warnings | Fixed 170 px log height scrolls (`:40`); the latest line stays visible |
| Narrow screens | Desktop | 360 px phone | Summary stacks (`:110`); the drawer goes full-width (`:88`); the grid scrolls sideways |

## 4. Offline and slow network, per screen

| Screen / region | Slow network | Offline |
|---|---|---|
| Home form (B) | **Starting…** on the button; after 10 s "Still working…"; after 30 s Error with **Try again** (idea kept) | **Check idea** disabled with "You're offline. Your idea will stay here." Typing is still allowed. |
| Recent reports (C) | Browser-stored, so instant | Works. Opening an uncached report shows the row D error. |
| Opening a report (D) | Skeleton → "Still working…" at 10 s → Error with Retry at 30 s | "You're offline. This report will load when you're back." It retries automatically on the `online` event. |
| Progress (E) | Events may arrive in bursts; stages never jump backwards. If no event arrives for 60 s: "Still working on {stage}…" | "Reconnecting…". On `online`, reload the report state and resume. The check keeps running on the server. |
| Finished report (F–J) | Already in the page | Everything already shown stays readable. Source links and downloads show "You're offline." |
| Evidence drawer (K) | Instant (data in page) | Quote readable; **Open source** shows "You're offline." |
| Download (L) | **Preparing…**, then Error with Retry at 30 s | Disabled with "You're offline." |
| First page load | The browser's own loading indicator | The browser's offline page. No service worker exists, and one is out of scope. |

## 5. Missing states in existing screens

Each row is a state the code doesn't render today, with where it should go.

| # | Region | Missing state | Evidence |
|---|---|---|---|
| 1 | A. Status indicator | Error (stays "…" if `/api/status` fails) and Loading delay | `index.html:118`, `:201–205`: no `.catch` |
| 2 | B. Idea form | Error for validation (no message for under 10 characters) | `index.html:238` |
| 3 | B. Idea form | Error for over 4,000 characters, server errors, and network failure on start (all end silently) | `index.html:247`, `:256–263`; `server.py:73` |
| 4 | C. Past runs | Loading; Empty distinct from Error (both look like an empty dropdown); Error | `index.html:207–212` |
| 5 | C. Past runs | Ideal at the extreme: failed runs display "null/100" | `index.html:210` |
| 6 | D. Opening a saved run | Loading and Error (no indicator; a failed fetch does nothing) | `index.html:214` |
| 7 | E. Progress | Error for a lost connection (silent stop, no Retry); slow-network "Still working…" | `index.html:256–263` |
| 8 | E/F. Failed check | Retry in place (banner shows, but there's no **Try again**) | `index.html:258`, `:282` |
| 9 | F–J. Report | Loading skeleton (results hidden until complete; no skeleton anywhere) | `index.html:240`, `:275` |
| 10 | G. Grid | Empty: grid card hidden when no projects, so the "No close projects found." row is unreachable | `index.html:289`, `:319` |
| 11 | G. Grid | Partial: rows only render when the whole check finishes | `index.html:261`, `:273` |
| 12 | H. Pivots | Error distinct from Empty: a pivot failure shows "No pivots generated." | `pipeline.py:370–371`, `index.html:327` |
| 13 | I. Rejected claims | Verifier-outage note in the region (top banner only) | `pipeline.py` verifier message; `index.html:330–338` |
| 14 | L. Download | Loading and Error (opens the raw server error page) | `index.html:183`, `server.py:61` |
| 15 | All | Offline detection and slow-network messages: none exist | whole file |
| 16 | M. Not found | The screen doesn't exist (proposed in 09.1) | — |

## Follow-ups (out of scope for this phase)

- **Build-phase work** to implement Section 5, in this order:
  - errors that are silent today (2, 3, 6, 7, 12);
  - empty vs. error distinctions (4, 10);
  - skeletons and partial grid streaming (9, 11);
  - offline handling (15).
- **Hackathon deadline (Nov 20, 8:59 PM PT).** Items 2, 3, 7, 8, 10 and 12 are small, self-contained changes to `index.html` and safe before submission. Items 9, 11, 15 and 16 depend on 09.1's routes and should wait.
- **Fix "null/100"** for failed runs in the Past runs list (item 5). It's a one-line display bug, recorded here rather than fixed in this documentation phase.
- **Server-side timeouts.** Row E's 60 s "Still working" assumes stage events keep coming; a single model call can block for up to 120 s (`llm.py:46`). A server heartbeat event would make the slow-network state reliable.
