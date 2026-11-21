# Prior Art — UI/UX Craft, 5-State Matrix & Conversion Polish (S.05)

Scanned 2026-11-20. The whole interface is one file, `src/priorart/static/index.html` (368 lines): styles `:7–110`, markup `:113–192`, script `:194–368`. There is no framework or build step, so every fix below is plain HTML, CSS and JavaScript in that file (or split out of it). Binding design docs: `docs/STATE_MATRIX.md`, `docs/INTERACTION_STANDARDS.md`, `docs/UX_ARCHITECTURE.md`.

**Measured today** in Chrome (Playwright), offline demo mode, with one full check at 375×812 and 1280×800:

| Measure | 375 px (phone) | 1280 px (desktop) |
|---|---|---|
| Cumulative layout shift, load → finished report | **0.0098** | **0.0026** |
| Horizontal page overflow | **8 px** (the page scrolls sideways) | 0 |
| Grid scroll width / visible width | **720 / 289 px** | 1094 / 1094 |
| Sample chip / outage selector / Past runs / close ✕ / citation link (W×H px) | 173×32 / 244×30 / 340×37 / 43×41 / **24×13** | same |
| Grid cells reachable by keyboard | **0** (`tabIndex` −1) | 0 |

| Contrast (WCAG 2.1, computed from the CSS tokens) | Ratio | Needed |
|---|---|---|
| "Done" text on its red cell (`:74`, 12.5 px) | 3.72 | 4.5 |
| "Open" text on its green cell (`:72`, coverage row) | 3.38 | 4.5 |
| "cut" text on its striped cell (`:76`) | 2.99 | 4.5 |
| **Check it** button while disabled (`:30`) | 3.40 | n/a, but reads as broken |
| Muted text on panels (`--muted`) | 5.76 / 5.34 | 4.5 ✓ |

Layout shift is already low, so the job there is to keep it low while adding streaming content (#01, #06). The bigger gaps are the two-minute wait, dead ends, phones, keyboard access and contrast.

## 5-state coverage

### #01 [P1] The two-minute wait shows only a scrolling log
- Location: progress card `src/priorart/static/index.html:136–140`; results hidden at start `:241`; every stream event turns into a log line `:248–255`; results appear only on `done` `:261`, `:275`.
- Risk: The product's best moment (watching existing projects and checked quotes appear) is hidden behind monospace log text for about two minutes. People can't tell how long it will take or whether anything useful is happening, and many leave before the report appears. `docs/STATE_MATRIX.md` defines a partial state that doesn't exist.
- Fix:
  - Show the results layout as soon as a check starts, with **geometry-matched skeletons**: a 210 px score block, three verdict lines, and grid rows at the real 44 px cell height. Use `.sk { background: linear-gradient(90deg, var(--panel2), var(--line), var(--panel2)); background-size: 200% 100%; animation: sk 1.2s linear infinite; border-radius: 8px; }`, disabled under reduced motion (#07).
  - Fill the skeleton as data streams in:
    - `plan` → the real feature column headers;
    - `triage` → one row per project, with skeleton cells;
    - `compared` → the claimed cells;
    - `verified` → final cells, with cut cells struck through as they happen.
  - Add an elapsed timer and "Usually about 2 minutes" under the stage chips.
  - Collapse the log into a `<details><summary>What it's doing</summary>` element.

### #02 [P1] A failed check is a dead end
- Location: error banner inside the progress card `src/priorart/static/index.html:138`, filled at `:256–259`; the failed stage chip `:107–108`, `:228`.
- Risk: After "Run failed." there is no button to retry, edit or report. Whether the user should try again in a minute or change the idea is left to guesswork, and a connection drop shows no message at all.
- Fix: Replace the banner with an inline error panel in the results area:
  - the user-facing cause, in the copy from `docs/INTERACTION_STANDARDS.md`;
  - **Try again** (re-submits the unchanged idea) and **Edit idea** (focuses the field with the text kept);
  - a reference id;
  - for a dropped connection, "Connection lost. The check keeps running." with a **Reconnect** button (S.02 #09).

  Use `role="alert"` and move focus to the panel heading.

### #03 [P2] Small surfaces fail silently: status pill and Past runs
- Location: `src/priorart/static/index.html:118` (pill starts as "…"), `:201–206` (filled with no error path), `:208–214` (Past runs loads and opens with no `.catch`).
- Risk: If `/api/status` or `/api/runs` fails, the pill shows "…" forever and Past runs stays empty with no explanation; opening a missing run throws mid-render.
- Fix: The pill renders three explicit states: "Checking…", "Ready · <models>" and "Can't reach the server" with a **Retry** link. Past runs shows "Couldn't load past runs. Retry" in place of its list. Both go through one `api()` helper (S.02 #10).

### #04 [P2] The first screen doesn't show what you get
- Location: header and input card `src/priorart/static/index.html:115–134`; sample chips `:125–126`; Past runs hidden in a native select `:132`.
- Risk: A first-time visitor (a judge included) sees a text box, two cryptic chips and a demo-failure selector. Nothing shows the overlap grid, the checked quotes or the novelty score, so the value of pressing the button is left unexplained.
- Fix: Under the input card, add a three-step strip ("Features → Existing projects with quotes → Novelty and open gaps") with a static, miniature, real example grid (4 projects × 4 features, from a saved demo report) labeled "Example". Rename the chips "Try: a research agent" and "Try: Prior Art itself". Move Past runs into a "Recent" list (#20). Hide the outage selector outside demo mode (S.03 #15).

### #05 [P2] Reports without results have no next step
- Location: no-score state `src/priorart/static/index.html:276–280` (shows "–" and an empty bar); the grid card is hidden when there are no projects `:289`, so the empty-grid message at `:319` can never appear; pivot and cut empty states `:327`, `:337`.
- Risk: "Insufficient evidence" and "Low evidence" reports read as broken: a dash, an empty bar, hidden sections and no suggestion of what to do.
- Fix: For `insufficient_evidence` and `failed`, replace the score block with an explanation card: what happened, then **Try a more specific description** (prefills the idea with a tip) and **Try again**. For `no_close_match`, keep the grid card visible with "No close projects found in N results. That's low evidence, not proof it's new." Point every empty section at its next action.

## Layout stability and motion

### #06 [P2] The page jumps to results and the header shifts as data arrives
- Location: forced scroll on finish `src/priorart/static/index.html:339` (`scrollIntoView({behavior: "smooth"})`) and on the checks chips `:300`; the progress card appears at `:242`; the status pill text and the Past runs select change width as they load `:118`, `:132`, `:201–212`.
- Risk: Someone reading the log or the page header when the check finishes is yanked down the page. CLS is low today (0.0098 on a phone), but streaming results (#01) would push content around unless space is reserved.
- Fix: Scroll only if the user hasn't scrolled since starting; otherwise show a floating "Report ready ↓" pill. Give the pill and the Recent control fixed widths (`min-width`) and fixed-height text. Skeletons from #01 reserve the final geometry. Add a Playwright test that fails if CLS exceeds 0.05 during a full check.

### #07 [P2] Motion ignores reduced-motion preferences
- Location: cell hover `transform: scale(1.04)` `src/priorart/static/index.html:72–73`; drawer slide `:88`; smooth scrolling `:300`, `:339`.
- Risk: Users who set "reduce motion" still get zooming cells, a sliding panel and long animated scrolls.
- Fix: `@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; } .cell:hover { transform: none; } }`, and pass `behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth"` to every scroll call. Replace the hover scale with a border-color change.

## Form ergonomics

### #08 [P1] The idea field gives no feedback and has no label
- Location: textarea `src/priorart/static/index.html:122` (placeholder only, no `<label>`, no `maxlength`, no counter); silent minimum `:238` (`if (idea.length < 10) { $("#idea").focus(); return; }`); server maximum 4,000 (`src/priorart/server.py:73`); the button says "Check it" `:124`.
- Risk: A short idea makes the button do nothing visible, an idea over 4,000 characters starts and then fails silently (S.02 #08), and screen readers announce an unlabeled field. The button label doesn't match the vocabulary in `docs/INTERACTION_STANDARDS.md`.
- Fix:
  - a visible `<label for="idea">Your idea</label>` and `aria-describedby` pointing to a hint and an error line;
  - validate on submit, and on blur only after a first submit (as the standard says);
  - a counter that appears from 3,500 characters and turns red past 4,000;
  - the error text under the field, with `aria-invalid="true"`;
  - **⌘/Ctrl+Enter** submits;
  - rename the button **Check idea**.

### #09 [P1] Opening a past run erases the idea being typed
- Location: `src/priorart/static/index.html:214` (`$("#idea").value = r.idea` when a past run is chosen); nothing saves the draft across reloads.
- Risk: A user writes a careful paragraph, glances at a past run, and their draft is replaced with no way back. A reload loses it too.
- Fix: Show a past report's idea inside the report (a quoted "Idea checked" block) instead of writing it into the input. Save the draft to `sessionStorage` on input (debounced 300 ms) and restore it on load. Fill the input from a report only through an explicit **Edit and re-check** button.

### #10 [P1] While a check runs, controls stay live and the button just fades
- Location: the only busy state is `disabled` at 50% opacity `src/priorart/static/index.html:30`, `:240` (3.40:1 contrast, no label change); the Past runs select `:132`, `:214` and the outage selector `:127` stay active during a run.
- Risk: The faded button reads as broken rather than busy. Choosing a past run mid-check renders that report, and then the running check's `done` event (`:261`) silently replaces it. There's no way to cancel.
- Fix: Busy state: the button label changes to "Checking… 0:42" with `aria-busy="true"` and full contrast. A **Cancel** button closes the stream and calls the cancel endpoint once it exists. While running, opening another report detaches the live check into Recent ("Running…") instead of being overwritten. The outage selector is locked during a run.

## Phones and tablets

### #11 [P1] The whole page scrolls sideways on phones
- Location: the Past runs select `src/priorart/static/index.html:32` (`max-width: 340px; margin-left: auto`) inside a 291 px content box at 375 px wide (`.wrap` padding `:17` plus card padding `:24`), markup `:132`.
- Risk: Measured: the page is 8 px wider than the screen at 375 px, so every vertical swipe also nudges the page sideways, which feels broken on the first touch.
- Fix: `select { max-width: 100%; }` and, below 600 px, `.row > select { flex: 1 1 100%; margin-left: 0; }`. Add `html, body { overflow-x: clip; }` only as a backstop, after fixing the cause. Add a Playwright check at 320, 375 and 414 px that `scrollWidth === innerWidth`.

### #12 [P1] The overlap grid is unusable on a phone
- Location: `src/priorart/static/index.html:61–62` (`overflow-x: auto`, `min-width: 720px`), `:66` (250 px project column); measured 720 px of grid in a 289 px viewport.
- Risk: The core output shows a project name and less than one feature column, with no cue that it scrolls and no way to compare a project across features without losing its name.
- Fix: Below 760 px:
  - make the project column sticky: `.grid td.name, .grid th.proj { position: sticky; left: 0; background: var(--panel); z-index: 1; width: 132px; }`;
  - add edge fades as a scroll hint (`.gridwrap` with `mask-image: linear-gradient(90deg, #000 85%, transparent)` while scrollable);
  - shorten headers to F1…F7, with full names in a legend below.

  Below 480 px, switch to a card per project listing its feature scores as labeled rows, each opening the drawer.

### #13 [P1] Touch targets are below 44 px
- Location: sample chips `src/priorart/static/index.html:31` (measured 173×32), outage selector `:109` (244×30), Past runs `:32` (340×37), drawer close `:90` (43×41), citation links `:102` (24×13).
- Risk: On phones the citation links, the product's proof, are 13 px tall and hard to hit, and the chips and close button miss on the first tap.
- Fix: A `--tap: 44px` token with `min-height: var(--tap)` on buttons, chips and selects; drawer close 44×44; citation links get `padding: 10px 6px; margin: -10px -4px` so the hit area grows without changing the text layout. Assert the sizes in the Playwright test.

## Keyboard, contrast and accessibility

### #14 [P1] The evidence can't be reached by keyboard and focus is invisible
- Location: grid cells are `<td>` with click handlers `src/priorart/static/index.html:315`, `:323` (measured `tabIndex` −1, zero focusable cells); rejected-claim rows and check chips are `<div>`s with click handlers `:297–300`, `:334`, `:338`; the only focus style is `textarea:focus` `:26`.
- Risk: Keyboard and screen-reader users can't open a single quote, which is what the product sells. Sighted keyboard users can't see where focus is on buttons and links styled by the page.
- Fix:
  - wrap each scored cell's content in `<button class="cellbtn" aria-label="Project 3, Feature F2: done. Show evidence">`;
  - make `.cutitem` and the cut chips real `<button>`s;
  - a global `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: inherit; }` and `:focus:not(:focus-visible) { outline: none; }`;
  - support arrow-key movement within the grid (roving `tabindex`) so it isn't 50 Tab stops.

### #15 [P1] The evidence drawer doesn't behave like a dialog
- Location: `src/priorart/static/index.html:189–192` (`<aside aria-live="polite">`, no role or label), open and close `:216–217`, `:342–368`.
- Risk: Opening it doesn't move focus, so screen-reader users don't know it opened, and `aria-live` makes the whole drawer be read out on every change. Escape closes it but leaves focus on nothing. The page behind still scrolls and stays clickable, and there's no backdrop to click away.
- Fix: `<aside role="dialog" aria-modal="true" aria-labelledby="dtitle">` (remove `aria-live`); on open, remember the trigger, move focus to the drawer's heading, and trap Tab inside it; on close, return focus to the trigger. Add a backdrop (`.scrim`) that closes on click, and `overflow: hidden` on the body while open. A `<dialog>` element with `showModal()` gives most of this natively.

### #16 [P1] Scores rely on color and miss contrast minimums
- Location: cell text colors `src/priorart/static/index.html:72`, `:74`, `:76` (computed 3.38:1, 3.72:1, 2.99:1 for 12.5 px text); open cells have no text at all (`LABEL[0] = ""` at `:198`, used at `:314`); the novelty number is colored by band `:278`; a *verified* claim uses the red "taken" style `:95`.
- Risk: Color-blind users can't tell "open" from "partly" from "done", and the small labels fail WCAG AA. A green checkmark-style "Verified" shown in red reads as an error, which confuses the meaning of the whole grid.
- Fix: Raise each label to at least 4.5:1 (for example `--taken-ink: #ff8a8d` on the red cell, `--open-ink: #7ff0b8`, `--cut-ink: #b4bac3`); give every cell a text label and an icon: "● Done", "◐ Partly", "○ Open", "✂ Cut". Style the *verified* status neutrally ("✓ Verified" in `--text` on `--panel2`) and keep red for "already taken" meaning only. Add a contrast check over the token pairs to the tests.

## Visual system

### #17 [P2] Colors and spacing live outside the token set
- Location: raw values `src/priorart/static/index.html:29` (`#06140d`), `:38`, `:55`, `:72`, `:76`, `:107` (one-off `rgba()` versions of token colors), `:97` (`#c3c8cf`), `:99` (`#f6d6a8`), `:100` (`#f5c2c4`); 14 lines with inline `style="..."` attributes, for example `:138`, `:143`, `:158`, `:159`, `:167`, `:183`, `:309`, `:329`.
- Risk: The tokens at `:8–13` cover only half of what's on screen, so every new state picks a new shade, banners and statuses drift from the palette, and a light theme or contrast fix (#16) has to be done line by line. Inline styles also block a strict CSP (S.03 #06).
- Fix: Expand the tokens into semantic roles (`--surface-1/2/3`, `--border`, `--ink-1/2/3`, `--state-taken|partial|open|cut-{bg,ink,border}`, `--focus`, `--space-1…6` on a 4 px grid, `--radius-s/m/l`). Replace every raw color and inline style with a class; add a test that fails on `style="` in the page and on any hex color outside `:root`.

### #18 [P2] Typography has no scale and an inverted hierarchy
- Location: 14 distinct font sizes in the stylesheet (10.5, 11, 12, 12.5, 13, 13.5, 14, 14.5, 15, 18, 19, 20, 30 and 64 px); section titles are 15 px uppercase muted `h2`s `src/priorart/static/index.html:58`, smaller and quieter than the 19 px verdict `:50` and the 20 px drawer heading `:91`.
- Risk: The report reads as a dashboard of captions: nothing tells the eye that the verdict, then the grid, then the open space are the story. The many near-identical sizes look accidental.
- Fix: A six-step scale (`--fs-xs 12`, `--fs-s 13`, `--fs-m 15`, `--fs-l 18`, `--fs-xl 24`, `--fs-display 56`) with matching line heights and `font-variant-numeric: tabular-nums` for scores and counts. Section titles become 18 px semibold in `--ink-1` with the uppercase label as a small eyebrow above; the verdict 24 px. Use real heading levels in order (h1 → h2 → h3).

### #19 [P2] Long text breaks badly
- Location: sources `src/priorart/static/index.html:103` (`word-break: break-all` splits URLs mid-word), feature headers `:63`, `:305` (long names make tall headers; descriptions only in `title` tooltips, invisible on touch), project names in a fixed-layout table `:62`, `:66–68`.
- Risk: URLs read as broken text, a single long feature name makes the header row three times taller, and touch users never see feature descriptions.
- Fix: `overflow-wrap: anywhere` instead of `break-all`, and show the URL as its host plus a shortened path (`github.com/org/repo/…`) with the full URL on the link. Clamp headers to two lines (`-webkit-line-clamp: 2`) with full name and description in a features legend under the grid. Clamp project names to two lines with the full name in the drawer.

### #20 [P2] A finished report has no next step
- Location: the only action is a plain "Download report (.md)" link `src/priorart/static/index.html:183`; pivots are text only `:325–327`; Past runs is a native select `:132`; there's no "New check" control once results show.
- Risk: The report ends without an obvious next action: no way to share it, try a pivot, or start fresh. That's the moment users would bring the product to someone else.
- Fix: An action bar under the verdict: **Copy link** (once reports have URLs, S.02 #06), **Download Markdown**, **New check** (clears the report, keeps the draft). Each pivot gets **Check this pivot** (prefill only). Replace the select with a "Recent" list of cards (status, novelty, summary, time). Confirm copies with a short toast (`role="status"`).

## Suggested batches

| Batch | Items | Notes |
|---|---|---|
| **A: before judging** | #08, #09, #10, #11, #13, #16 | Small CSS and handler changes; fix what a judge hits in the first minute |
| B: the waiting experience | #01, #02, #05, #06 | Streaming skeleton and real error and empty states |
| C: keyboard and dialog | #14, #15, #07 | Accessibility pass |
| D: visual system | #17, #18, #19 | Tokens and type scale |
| E: first screen and next steps | #03, #04, #20 | Conversion polish |
