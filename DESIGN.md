# Design system (as built, build step 1)

Source of truth: `ui/src/tokens.css` (tokens) and `ui/src/app.css` (shared components). Dark only, mint accent, monochrome surfaces so the images are the colour. One mint (primary) action per view.

## Tokens

| Token | Value | Use |
|---|---|---|
| `--bg` | `#0a0a0b` | page |
| `--surface` | `#111113` | cards, composer, rows |
| `--raised` | `#1a1a1d` | chips, hover fill, progress track |
| `--float` | `#232327` | popovers, menus, toasts, tooltips |
| `--line` / `--line2` | white 7% / 12% | hairlines / control borders |
| `--text` / `--text-soft` | `#ededef` / `#c9c9cf` | text / prompt text |
| `--muted` / `--faint` | `#8e8e96` / `#7c7c86` | secondary text (5.7:1) / quiet icons (≥4.5:1 on surface) |
| `--mint` `--mint-hover` `--mint-ink` | `#6ee7b7` `#86eec4` `#062a1e` | accent, primary button, text on mint |
| `--mint-dim` / `--mint-line` | mint 10% / 38% | locked fill / locked border, focus glow |
| `--amber` (+ `-dim`, `-line`) | `#f5b451` | needs attention, ComfyUI offline |
| `--red` (+ `-dim`, `-line`) | `#ff8a80` | failed, destructive |
| Radii | 6 / 8 / 9 / 12 / 14 | chip-inner / chips and inputs / buttons / popovers and rows / cards |
| `--ease` | `cubic-bezier(.16,1,.3,1)` | every transition (JS twin: `ease` in `motion.js`) |
| Shadows | `--shadow-pop` `0 8px 24px /.45`, `--shadow-overlay` `0 24px 64px /.6` | popovers, image viewer |

Fonts: Geist (UI) and Geist Mono (numbers, seeds, sizes, paths), self-hosted through `@fontsource-variable/*`. `font-variant-numeric: tabular-nums` on `body`.

## Type scale

Page title 22/600, -0.02em · section title 15/600 · card title 15/600 · body 14 · prompt text 13.5 / 1.55 · chip and control text 13 · secondary and status 12.5 · tooltip title and field label 11.5–12 / 500 · mono 12.5.

## Layout

- Shell: left rail (52px icons under 1000px, 200px with labels from 1000px), content scrolls. Rail items: Plan, Queue (mint dot while jobs are queued or running), Gallery, Library; Settings pinned to the bottom.
- `.page`: padding 28/24 (32 from 1000px), 28px gap, max width 1280. `.page.with-side` adds a 280px sticky Queue column (Plan only).
- Page head: title left, ComfyUI status right (`ComfyStatus`: mint dot ready, amber dot + Start button offline).
- No eyebrow labels; one mint button per view (Plan: "Run N images"; with nothing to run the empty state's "Add job"; Gallery viewer: "Use as new job").

## Components

- **Buttons**: `.btn` 32px (`.sm` 28), 1px `--line2` border, hover `--raised`, `:active` scale .97; `.primary` mint; `.danger` red on hover. `.icon-btn` 32px ghost. Disabled = 45% opacity; a disabled `.primary` is neutral instead (`--raised` fill, `--muted` text, `--line2` border, no hover change), so a button that cannot be used yet does not read as a faded green. Actions the LLM carries out (Plan, Build card, Build N missing cards) carry the filled `spark` icon, centred on the 24px grid; other buttons have an icon only where it names the action (Retry, Import, Add).
- **Chip** (`FieldChip.svelte`): 30px, `--raised`. Locked = `--mint-dim` fill + `--mint-line` border + a lock button on the right (click unlocks). Padding-right animates when locking. Hover tooltip (`.tip`, 250ms delay) says who set the value. `.add` = dashed. Click opens a popover (`.pop`, 264px): editor, a Lock switch (default on: saving locks), Save.
- **Prompt row** (`.p`): index, text clamped to 2 lines (button), click edits in an auto-growing textarea (Enter saves, Shift+Enter newline, Esc cancels); lock button appears on hover, mint when locked. Saving a prompt locks it. Per-row locks are stored as the extra job field `prompt_locks`.
- **Details** disclosure: `grid-template-rows 0fr → 1fr` over .45s; holds negative, seed strategy, scheduler, hires.
- **Job card** (`.job`): surface, 14px radius, 16/18 padding. Header = workflow title, count (ghost chip), `…` menu (Duplicate, Delete).
- **Add job panel** (`.composer`): same surface the request box will use in step 4; revealed with the same grid-rows trick.
- **Need** message (`.need`): amber text + dashed `.pick` buttons; used for jobs that cannot run yet (empty prompt).
- **Queue row** (`.q` / `QueueRow.svelte`): 44×56 thumbnail (`object-fit: cover`; while the job generates it shows the newest step preview of the image in progress, swapped in only after it has loaded; with none yet the placeholder or the first finished image; the finished thumbnail once the image is done; nothing is kept after a fail or cancel), title, state line, 3px mint progress (width transitions .8s; indeterminate until the first progress event), actions on the right. Full variant is bordered; running = mint border. On the Queue page the running row is a stage (`.q.stage`, `LivePreview.svelte`): the live picture leads the row, up to 52vh and half the width with its long side capped at 512px (ComfyUI's preview size), at the image's own aspect ratio, with a 2px mint bar on its bottom edge for the current image's progress; title, state, batch progress and job list sit beside it. Between images the last frame stays at 50% opacity. It stacks under 620px of width.
- **Banner** (`.banner`): amber for ComfyUI unreachable / paused, with the recovery button.
- **Gallery tiles**: justified rows (flex-grow = aspect ratio), 12px radius, 1px inset line that turns mint on hover or focus. Viewer = fixed overlay, image left, recipe panel (360px) right; stacks under 900px.
- **Gallery select and delete**: grid and workflow covers load cached WebP thumbnails (`/api/images/{id}/thumb`), the viewer loads the full file. `Select` in the page head turns tiles into toggles (mint check mark, mint 2px outline); deleting asks inline ("Delete these N images?" Cancel / Delete) and leaving tiles shrink their width so neighbours close the gap. The viewer asks the same way (Delete key arms, Delete again confirms, Esc cancels) and moves to the next image. Native `confirm()` is never used.
- **Workflow card** (Library): 4:3 cover thumbnail on top (the pinned image, else the newest one; dashed-free placeholder with an image icon when there are none), title, kind badge, ready or missing state. Selecting a card opens its slots and missing files in a panel under the grid. `Re-check` re-reads models from the running ComfyUI (also runs quietly when the page opens).
- **Workflow picker** (Plan, `WorkflowPicker.svelte`): one snapping horizontal row of 188px tiles, a radio group with arrow/Home/End keys and a roving tab stop. Tile = 4:3 cover thumbnail (placeholder: image or pencil glyph on a soft gradient), kind badge bottom-left, readable name plus variant line ("Name - variant (note)" is split for display only; the full title is the tooltip and shows next to the "Workflow" label). Selected = 2px mint ring and a mint check; focus adds a soft mint halo. Edge fades and chevrons appear only when the row overflows. The mouse wheel scrolls the row sideways (`wheelScroll.js`, also on the card's slots table): a smooth glide, snap off while it runs, and the wheel goes back to the page when the row can't move further that way; shift+wheel, trackpad sideways swipes and pinch-zoom are left alone. In the request box only workflows the planner can use are listed (runnable, with an approved or allowed draft card); a quiet line says how many are left out, with a link to the Library.
- **LoRA toolbar** (Library, `LoraCatalogue.svelte`, `FilterMenu.svelte`): one row of 32px controls on an 8px gap, no filter rows of chips. Search (`.field`, 9px radius, takes the free width, 240px minimum, 420px maximum on a wide page); a status switch (`.seg`: All, Approved, Draft, No card, each with its count in muted 11.5px; one choice, the chosen segment is `--raised` with `--text`, never mint; shown only when two statuses exist); then Kind, Family, Tags and Sort as `.btn` list buttons with a chevron. A list button opens a popover (`--float`, `--line2`, 12px radius, `--shadow-pop`, 248px, 184px for Sort, `pop` motion, `dismissable`): rows of 30px with a check mark, the value and its count; the Tags list scrolls inside 284px. A button with a choice shows its count (`Kind 2`) and a brighter border, no fill; Sort reads `Sort: Name`. Arrow keys, Home and End move through the rows, Enter or Space chooses, Esc closes only the list and returns focus to its button, Tab leaves and closes it. Sort closes on a pick. The groups wrap on purpose: from 1120px of content width everything shares one row (Kind to Sort at the right end); below that the search and status switch take the first row and Kind, Family, Tags and Sort the second (Sort at the right end); below about 550px the search has a row of its own. While anything is filtered, a quiet line under the toolbar (the `.reveal` grid-rows trick) reads "Showing N of M" with a 24px removable chip per chosen kind, family or tag, and "Clear filters". The header keeps the title, "N of M approved" and, right-aligned, Build N missing cards (only when some are missing), Select and Rescan, all `.btn`; they take their own row when they do not fit beside the title.
- **Brand mark** (`Logo.svelte`, `ui/public/logo.svg`): mint rounded tile, ink "C" ring with a dot in its opening. `launcher/make_icons.py` draws the same geometry into every PNG, `favicon.ico` and `launcher/icon.ico` (16 to 256 px). The launcher uses `launcher/icon.ico` as the app window's title-bar and taskbar icon.
- **Empty state** (`.empty`): dashed border, icon tile, heading, one sentence, one button. **Skeleton** while loading.
- **Toast**: bottom centre, `--float`, optional mint action.
- **Icons**: `Icon.svelte`, inline SVG, 24px grid, stroke 1.6, round caps, `currentColor`. No emoji or glyph icons.

## Motion

- One orchestrated entrance: job cards use `.arrive` (opacity 0→1, translateY 8→0, blur 4→0, .8s, staggered by 110ms). Implemented as `backwards` fill, so the resting state is the visible one.
- Popovers, menus, toasts, viewer: opacity + translate (+ scale .97–.98), 240–320ms, same easing (`pop` in `motion.js`). Removing a job card fades and collapses it (`leave`).
- Keyboard and wheel: Esc closes the Plan add-job panel, the Library panels and import list, and the Gallery viewer; the viewer also takes arrows, Home/End and the wheel. Ctrl+S saves Settings. A new page opens at the top and sets the window title.
- Chip lock, switches, progress, disclosure, hover fills: 200–450ms on `--ease`. No other hover animation.
- `prefers-reduced-motion`: all animation and transition durations collapse to ~0 (global rule in `app.css`; JS transitions return duration 0).

## Craft floor, as implemented

Selection themed (mint on mint-ink), `caret-color` mint, thin dark scrollbars, mint 2px `:focus-visible` rings, themed number spinners and `color-scheme: dark` for native selects. Every list has loading, empty and error (toast) states; destructive or disabled controls are visibly disabled. Contrast: body and muted text ≥4.5:1 on `--bg`/`--surface`; amber and red text ≥7:1.

## Rules for new pages

Reuse the tokens and classes above, put the page's one primary action in the section head, keep text low-density, show state with the chip lock / dot language rather than new colours, and add no hover motion beyond background and border fades.

## Explaining the app (added after build step 2)

- **InfoTip** (`InfoTip.svelte`): a 13px outline "i" in `--faint`, quiet until hovered. Opens on hover (140ms delay), keyboard focus and tap; Esc closes; `aria-describedby` points at the bubble. The bubble is `position: fixed` (never clipped by cards or disclosures) in the `.tip` style, 260px max, above the icon or below it near the top of the window. Optional bold title. Used beside section titles, setting labels and buttons whose effect isn't obvious; never inside another button.
- **Settings** (`Settings.svelte`, `SettingRow.svelte`, `TagInput.svelte`): groups are surface cards of rows (label + InfoTip left, control right, hint or red error below). An edited row gets a small mint dot. A sticky save bar (`--float`, overlay shadow) appears only while something is unsaved: "N unsaved changes", Discard, Save changes (the one mint button). Errors come from `PUT /api/config` per field and clear when that field is edited. Advanced LLM settings sit in a disclosure. "Local LLM" carries a short how-to for adding a language model (four steps, no links or model names) and an "Open models folder" `.btn` (folder icon, never mint): while the chosen model's file is missing it is an amber `.banner` above the card, otherwise a quiet "Add another model" disclosure under it. "AI agents (MCP)" is a card of two rows (address, JSON config collapsed by default), each snippet in an inset `--bg` box with a ghost copy button that turns into a mint check for 1.6s; a muted line under it states the limits. host, port and data_dir are shown read-only under "Set when the app starts".
- **How it works** (`pages/Guide.svelte`, rail item above Settings): four numbered steps joined by a hairline (Library, Plan, Queue, Gallery; Plan carries Now / Next tags), a mint-tinted note that automatic planning is next, the LLM and two-phase GPU notes, and a plain list of six terms. The Plan page shows a dismissible one-line hint to it (`localStorage` key `cp.hint.how`).

## Planning UI (build step 4)

- **Request box** (`RequestBox.svelte`): `--surface` panel, 15px text, mint caret, mint focus ring (`--mint-line` border + 4px `--mint-dim` glow). The box starts with the workflow choice: until one is chosen "Choose a workflow" and the tiles are open; afterwards one 36px line (32x26 cover, title, chevron, InfoTip) that unfolds the tiles (the `.reveal` grid-rows trick) and folds again 380 ms after a pick. The picker's scroll arrows sit in that line. A hairline separates the text from the bar: Add image (image-edit workflows only, + InfoTip), `Ctrl+Enter` hint in mono ("Choose a workflow to plan with" while none is chosen), Plan (mint when no card is ready to run, otherwise a normal button so Run stays the one primary action). The box is never disabled by a running plan: while one is active the button reads "Queue plan" (same spark icon) and the new request waits in line; Add image stays available. Attached images are small chips with a thumbnail. An amber note appears when no workflow is usable.
- **Plan status** (`PlanStatus.svelte`): five points on a hairline (queued, waiting, loading, planning, done); finished steps are mint with the connecting line filling, the current one pulses. Done = mint border, failed = red tint with the reason and links to Library and Settings. A line under the track says what happens to the queue. The clock counts from when the plan left the line (`started_at`) and is hidden for a plan that has not started. Under the request box the plan being worked on has this full status; plans waiting in the line are compact one-line rows below it (`QueuedPlan.svelte`, `--surface` with a `--line` border): the request clipped to one line ("Re-plan N jobs" or "Change a job: ..." for re-plans and tweaks), "Next" or "N ahead" in muted text, and an x icon button ("Remove from the line") that takes it out. A done summary fades after 15 s; a failed or stopped plan stays until dismissed; several can be visible at once, and each uses the existing `leave` motion. A plan removed from the line before it started leaves nothing behind.
- **Reasons** (`WhyLine.svelte`): 12.5px `--muted` one-liners with a 12px icon: sparkle = chosen by the LLM or the workflow, mint padlock = locked, amber = clamp note or issue. Shown under each prompt and, in "Settings and reasons", under each field.
- **Locks**: a locked chip is mint (as before); `.lock-mini` is the small padlock beside each field in the details; each prompt row has a padlock and a 2px mint bar at its left edge while locked. A chip with a clamp note or issue gets a 6px amber dot.
- **Busy card** (`.job.busy`): unlocked chips and prompts fade to 40 %, locked ones stay bright, a 2px mint indeterminate line runs along the card's top edge, and the header says "Re-planning around your locks".
- **Blocking issue**: amber text (with a `.pick` button where one action fixes it, e.g. Re-plan or Open Library); the card shows a "Needs attention" badge and its Run is disabled.
