<script>
  // The Library's LoRA section: a grid of cover tiles with search, filters, sorting, the user's tags and a Select mode.
  // Clicking a tile opens the LoRA's card editor. Build, approve and re-generate live in the editor; Rescan and
  // "Build missing cards" are in the header.
  import { tick } from 'svelte'
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import FilterMenu from './FilterMenu.svelte'
  import LoraCover from './LoraCover.svelte'
  import LoraEditor from './LoraEditor.svelte'
  import DeleteDialog from './DeleteDialog.svelte'
  import TagEditor from './TagEditor.svelte'
  import { api } from './api.js'
  import { isBuilding } from './cards.js'
  import { live, toast, toastError, refreshLoras, changeLoraTags } from './store.svelte.js'
  import { fade, dismissable } from './motion.js'
  import { facet, loraName, matchesQuery, passes, searchText, sorted, statusOf, STATUS_TEXT, SORTS, tagsInUse } from './loraCatalogue.js'

  const STATE_TEXT = { new: 'New file', changed: 'File changed', removed: 'File removed', missing: 'File not found' }
  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1)

  let q = $state('')
  let filters = $state({ kinds: [], families: [], tags: [], statuses: [] })
  let sortBy = $state('name')
  let selecting = $state(false)
  let picked = $state([]) // LoRA file names chosen in Select mode
  let openKey = $state(null) // the editor is open for this LoRA
  let shownKey = $state(null) // last opened, kept while the editor fades out
  let tagFor = $state(null) // the tag popover is open on this tile
  let popRight = $state(false)
  let scanning = $state(false)
  let queuing = $state(false)
  let tagBusy = $state(false)
  let closeBtn = $state(null)
  let deleting = $state(null) // the LoRA whose delete dialog is open

  const entries = $derived(live.loras)
  const lora = $derived(entries.find((l) => l.key === (openKey ?? shownKey)))
  const haystack = $derived(new Map(entries.map((e) => [e.key, searchText(e)])))
  const shown = $derived(sorted(entries.filter((e) => passes(e, filters) && matchesQuery(haystack.get(e.key) ?? '', q)), sortBy))
  const filtering = $derived(!!q.trim() || Object.values(filters).some((f) => f.length))
  const loraMissing = $derived(entries.filter((l) => !l.card && l.state === 'new'))
  const approved = $derived(entries.filter((l) => l.status === 'approved').length)
  const allTags = $derived(tagsInUse(entries))

  // Kind, Family and Tags are lists to pick from (several values may be chosen). A list is offered when it has two or
  // more values to choose between, or has a choice made; a chosen value that no LoRA has any more stays in it, so it can be unchosen.
  const withChosen = (values, chosen) => [...values, ...chosen.filter((c) => !values.some((v) => v.value === c)).map((value) => ({ value, count: 0 }))]
  const menus = $derived.by(() => {
    const list = [
      { id: 'kinds', label: 'Kind', chip: 'Kind', values: facet(entries, (e) => (e.card ? [e.card.kind] : [])) },
      { id: 'families', label: 'Family', chip: 'Family', values: facet(entries, (e) => (e.card ? [e.card.family] : [])) },
      { id: 'tags', label: 'Tags', chip: 'Tag', values: facet(entries, (e) => e.user_tags ?? []) },
    ]
    return list.filter((g) => g.values.length >= 2 || filters[g.id].length).map((g) => ({ ...g, options: withChosen(g.values, filters[g.id]) }))
  })
  // Status is one choice: All, or one of Approved, Draft, No card (only those that exist).
  const chosenStatus = $derived(filters.statuses[0] ?? '')
  const segments = $derived.by(() => {
    const found = facet(entries, (e) => [statusOf(e)])
    const list = withChosen(['approved', 'draft', 'none'].map((s) => found.find((x) => x.value === s)).filter(Boolean), filters.statuses)
    if (list.length < 2 && !chosenStatus) return []
    return [{ value: '', text: 'All', count: entries.length }, ...list.map((v) => ({ ...v, text: STATUS_TEXT[v.value] }))]
  })
  const sortOptions = Object.entries(SORTS).map(([value, label]) => ({ value, label }))

  function toggleFilter(group, value) {
    filters[group] = filters[group].includes(value) ? filters[group].filter((x) => x !== value) : [...filters[group], value]
  }
  const setStatus = (value) => (filters.statuses = value ? [value] : [])
  // The line under the toolbar while something is filtered. It keeps its last words while it folds away.
  const chips = $derived(menus.flatMap((g) => filters[g.id].map((value) => ({ group: g.id, value, text: `${g.chip}: ${value}` }))))
  let last = $state({ shown: 0, total: 0, chips: [] })
  $effect(() => {
    if (filtering) last = { shown: shown.length, total: entries.length, chips }
  })
  const line = $derived(filtering ? { shown: shown.length, total: entries.length, chips } : last)
  function clearFilters() {
    q = ''
    filters = { kinds: [], families: [], tags: [], statuses: [] }
  }

  // ----- the tile's status chip -----
  function chipOf(l) {
    if (isBuilding(live.cardBuilds[`lora:${l.key}`])) return { text: 'Writing', busy: true }
    const s = statusOf(l)
    return { text: STATUS_TEXT[s], cls: s === 'approved' ? 'ok' : s === 'draft' ? 'draft' : '' }
  }
  function lineOf(l) {
    if (l.state === 'removed' || l.state === 'missing' || l.state === 'changed') return { text: STATE_TEXT[l.state], warn: true }
    if (!l.card) return { text: STATE_TEXT.new, warn: false }
    return { text: `${cap(l.card.kind)} · ${l.card.family}`, warn: false }
  }

  // ----- opening the editor, Select mode -----
  async function openEditor(key) {
    openKey = openKey === key ? null : key
    if (!openKey) return
    shownKey = key
    await tick()
    closeBtn?.focus()
  }
  function closeEditor() {
    const key = openKey
    openKey = null
    if (key) tick().then(() => document.querySelector(`[data-lora="${CSS.escape(key)}"]`)?.focus())
  }
  /** The delete dialog closes and the LoRA is gone from the list: close its editor too. */
  function deleted() {
    const key = deleting
    deleting = null
    if (key === openKey) openKey = null
  }
  function press(l) {
    if (!selecting) return openEditor(l.key)
    picked = picked.includes(l.key) ? picked.filter((k) => k !== l.key) : [...picked, l.key]
  }
  function toggleSelecting() {
    selecting = !selecting
    picked = []
    tagFor = null
    if (selecting) openKey = null
  }
  // what is not on screen can't stay selected
  $effect(() => {
    const keys = new Set(shown.map((e) => e.key))
    if (picked.some((k) => !keys.has(k))) picked = picked.filter((k) => keys.has(k))
  })
  const pickedTags = $derived(facet(entries.filter((e) => picked.includes(e.key)), (e) => e.user_tags ?? []).map((f) => f.value))

  // ----- tags -----
  async function changeTags(keys, add, remove) {
    tagBusy = true
    try {
      await changeLoraTags(keys, add, remove)
    } catch (e) {
      toastError(e)
    } finally {
      tagBusy = false
    }
  }
  function toggleTags(l, cell) {
    if (tagFor === l.key) return (tagFor = null)
    popRight = cell.getBoundingClientRect().left + 280 > innerWidth - 12
    tagFor = l.key
    tick().then(() => document.getElementById(`lc-tags-${l.key}`)?.focus())
  }
  function closeTags(e) {
    const key = tagFor
    tagFor = null
    if (key && e?.type === 'keydown') document.querySelector(`[data-tagbtn="${CSS.escape(key)}"]`)?.focus()
  }

  // ----- header actions -----
  async function rescan() {
    scanning = true
    try {
      await refreshLoras(true)
      toast('Looked at the LoRA folders again')
    } catch (e) {
      toastError(e)
    } finally {
      scanning = false
    }
  }
  /** Queue a build for each LoRA that has no card; they run one after another with the LLM loaded once. */
  async function buildMissing() {
    queuing = true
    try {
      for (const l of loraMissing) live.cardBuilds[`lora:${l.key}`] = await api.buildCard('loras', l.key)
      toast(`Writing ${loraMissing.length} ${loraMissing.length === 1 ? 'card' : 'cards'}. The queue waits for the current image.`)
    } catch (e) {
      toastError(e)
    } finally {
      queuing = false
    }
  }
  /** Esc closes the editor; typing in a field is left alone. */
  function onkey(e) {
    if (e.key !== 'Escape' || e.defaultPrevented || e.target.closest?.('input, textarea, select')) return
    if (openKey !== null) closeEditor()
    else if (selecting) toggleSelecting()
  }
</script>

<svelte:window onkeydown={onkey} />

<section class="stack lg">
  <div class="section-head lc-head">
    <h2>LoRAs</h2>
    <InfoTip title="LoRAs" text="Small add-on models that give a workflow a style, character or pose. The app finds them in ComfyUI's loras folders and builds a card for each, with trigger words and a suggested weight, so the planner can pick them. Give them tags of your own to find them again." />
    {#if entries.length}<span class="n">{approved} of {entries.length} approved</span><InfoTip text="Only approved cards are used when planning. Open a LoRA to check its card and approve it." label="About approved LoRAs" />{/if}
    <div class="lc-acts">
    {#if loraMissing.length}
      <button class="btn" onclick={buildMissing} disabled={queuing} title="Write draft cards for the LoRAs that have none"><Icon name="spark" />Build {loraMissing.length} missing {loraMissing.length === 1 ? 'card' : 'cards'}</button>
    {/if}
    {#if entries.length}
      <button class="btn" onclick={toggleSelecting} aria-pressed={selecting} class:primary={selecting} title="Choose several LoRAs to tag them together"><Icon name="check" />{selecting ? 'Done' : 'Select'}</button>
    {/if}
    <button class="btn" onclick={rescan} disabled={scanning} title="Look at the LoRA folders again for new, changed and removed files">
      <span class:spin={scanning} style="display:grid"><Icon name="retry" /></span>Rescan
    </button>
    </div>
  </div>

  {#if !live.ready}
    <div class="skeleton" aria-hidden="true"></div>
  {:else if !entries.length}
    <div class="empty compact fade-in">
      <div class="glyph"><Icon name="box" /></div>
      <h3>No LoRAs found</h3>
      <p>Put .safetensors LoRA files in a ComfyUI loras folder and press Rescan.</p>
    </div>
  {:else}
    <div class="lc-tools">
      <div class="lc-bar">
        <div class="lc-search">
          <Icon name="search" />
          <input class="field" type="search" aria-label="Search LoRAs" placeholder="Search name, words, tags" bind:value={q} autocomplete="off" spellcheck="false" />
        </div>
        {#if segments.length}
          <div class="seg lc-seg" role="group" aria-label="Filter by status">
            {#each segments as seg (seg.value)}
              <button type="button" aria-pressed={chosenStatus === seg.value} onclick={() => setStatus(seg.value)}>{seg.text}<span class="lc-n">{seg.count}</span></button>
            {/each}
          </div>
        {/if}
        <div class="lc-pickers">
          {#each menus as g (g.id)}
            <FilterMenu label={g.label} options={g.options} selected={filters[g.id]} tall={g.id === 'tags'} onpick={(v) => toggleFilter(g.id, v)} />
          {/each}
          <span class="lc-sort"><FilterMenu label="Sort" options={sortOptions} selected={[sortBy]} single onpick={(v) => (sortBy = v)} /></span>
        </div>
      </div>
      <div class="reveal" class:open={filtering}>
        <div>
          <div class="lc-active" inert={!filtering}>
            <span class="lc-showing" aria-live="polite">Showing {line.shown} of {line.total}</span>
            {#each line.chips as c (c.group + '/' + c.value)}
              <button type="button" class="lc-chip" aria-label="Remove filter {c.text}" onclick={() => toggleFilter(c.group, c.value)}><span>{c.text}</span><Icon name="x" /></button>
            {/each}
            <button type="button" class="lc-clear" onclick={clearFilters}>Clear filters</button>
          </div>
        </div>
      </div>
    </div>

    {#if selecting}
      <div class="lc-bulk" role="region" aria-label="Tag the selected LoRAs">
        <div class="lc-row">
          <strong>{picked.length} selected</strong>
          <button class="btn sm" onclick={() => (picked = shown.map((e) => e.key))} disabled={!shown.length || picked.length === shown.length}>Select all {shown.length} shown</button>
          <button class="btn sm" onclick={() => (picked = [])} disabled={!picked.length}>Clear selection</button>
          {#if !picked.length}<span class="muted">Click LoRAs to choose them, then add a tag.</span>{/if}
        </div>
        <TagEditor tags={pickedTags} all={allTags} busy={tagBusy} off={!picked.length} label="Tags on the selected LoRAs" id="lc-bulk-tag"
          onadd={(list) => changeTags(picked, list, [])} onremove={(t) => changeTags(picked, [], [t])} />
      </div>
    {/if}

    {#if !shown.length}
      <div class="empty compact fade-in">
        <div class="glyph"><Icon name="search" /></div>
        <h3>No LoRA matches</h3>
        <p>Nothing fits the search and filters together.</p>
        <button class="btn" onclick={clearFilters}>Clear filters</button>
      </div>
    {:else}
      <ul class="lc-grid">
        {#each shown as l, i (l.key)}
          {@const chip = chipOf(l)}
          {@const line = lineOf(l)}
          {@const on = picked.includes(l.key)}
          {@const tags = l.user_tags ?? []}
          <li class="lc-cell" class:lc-up={tagFor === l.key}>
            <button
              class="lc-tile"
              class:open={openKey === l.key}
              class:picked={on}
              data-lora={l.key}
              aria-pressed={selecting ? on : undefined}
              aria-expanded={selecting ? undefined : openKey === l.key}
              aria-haspopup={selecting ? undefined : 'dialog'}
              aria-describedby={l.card?.what_it_does ? `lc-d-${i}` : undefined}
              onclick={() => press(l)}
            >
              <span class="lc-cover">
                <LoraCover entry={l} />
                <span class="lc-status {chip.cls ?? ''}">{#if chip.busy}<span class="spin" style="display:grid"><Icon name="loader" /></span>{/if}{chip.text}</span>
                {#if l.card?.what_it_does || tags.length}
                  <span class="lc-about" aria-hidden="true">
                    <span class="lc-about-text" id="lc-d-{i}">{l.card?.what_it_does || ''}</span>
                    {#if tags.length}<span class="lc-about-tags">{tags.join(' · ')}</span>{/if}
                  </span>
                {/if}
              </span>
              <span class="lc-meta">
                <span class="lc-name" title={l.key}>{loraName(l.key)}</span>
                <span class="lc-line" class:warn={line.warn}>{line.text}</span>
              </span>
            </button>

            {#if selecting}
              <span class="lc-mark" class:on aria-hidden="true"><Icon name="check" /></span>
            {:else}
              <button class="lc-delbtn" data-delbtn={l.key} aria-label="Delete {loraName(l.key)}" title="Delete this LoRA and its files" onclick={() => (deleting = l.key)}><Icon name="trash" /></button>
              <div class="lc-tagwrap" use:dismissable={(e) => tagFor === l.key && closeTags(e)}>
                <button class="lc-tagbtn" class:has={tags.length} data-tagbtn={l.key} aria-label="Tags for {loraName(l.key)}{tags.length ? `: ${tags.join(', ')}` : ''}"
                  aria-expanded={tagFor === l.key} aria-haspopup="dialog" onclick={(e) => toggleTags(l, e.currentTarget.closest('.lc-cell'))}>
                  <Icon name="tag" />{#if tags.length}<span>{tags.length}</span>{/if}
                </button>
                {#if tagFor === l.key}
                  <div class="lc-pop" class:right={popRight} role="dialog" aria-label="Tags for {loraName(l.key)}">
                    <TagEditor id="lc-tags-{l.key}" {tags} all={allTags} busy={tagBusy} label="Your tags on {loraName(l.key)}"
                      onadd={(list) => changeTags([l.key], list, [])} onremove={(t) => changeTags([l.key], [], [t])} />
                  </div>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</section>

{#if openKey !== null && lora}
  <div class="sheetbox" transition:fade={{ duration: 180 }}>
    <button class="sheet-scrim" tabindex="-1" aria-label="Close" onclick={closeEditor}></button>
    <div class="sheet" role="dialog" aria-modal="true" aria-label="LoRA {loraName(lora.key)}">
      <div class="sheet-head">
        <h3 class="mono" title={lora.key}>{lora.key}</h3>
        <span class="spacer"></span>
        <button class="icon-btn danger" onclick={() => (deleting = lora.key)} aria-label="Delete {loraName(lora.key)}" title="Delete this LoRA and its files"><Icon name="trash" /></button>
        <button class="icon-btn" bind:this={closeBtn} onclick={closeEditor} aria-label="Close details"><Icon name="x" /></button>
      </div>
      <LoraEditor entry={lora} />
    </div>
  </div>
{/if}

{#if deleting !== null}
  <DeleteDialog kind="lora" target={deleting} name={loraName(deleting)} ondone={deleted} onclose={() => (deleting = null)} />
{/if}

<style>
  .lc-head { flex-wrap: wrap; row-gap: 8px; }
  .lc-head :global(.n) { white-space: nowrap; }
  /* the actions stay together on the right; when they don't fit beside the title they take the next row */
  .lc-acts { flex: 1 1 auto; display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }
  .lc-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 10px; min-width: 0; }
  .lc-tools { container-type: inline-size; }
  /* one toolbar, every control 32px tall; it wraps by group when the page is narrow */
  .lc-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .lc-search { position: relative; flex: 1 1 240px; min-width: 240px; display: flex; align-items: center; }
  .lc-search :global(svg.i) { position: absolute; left: 11px; color: var(--muted); pointer-events: none; }
  .lc-search .field { width: 100%; padding-left: 34px; border-radius: var(--r-lg); }
  .lc-seg { border-color: var(--line2); }
  .lc-seg button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
  .lc-n { color: var(--muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
  /* Kind, Family, Tags and Sort stay together: beside the search on a wide page, otherwise on their own row, sort at its right end */
  .lc-pickers { flex: 1 1 100%; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  .lc-sort { margin-left: auto; display: inline-flex; }
  @container (min-width: 1120px) {
    .lc-search { max-width: 420px; }
    .lc-pickers { flex: 0 0 auto; margin-left: auto; }
  }
  .lc-active { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; padding-top: 10px; }
  .lc-showing { color: var(--muted); font-size: 13px; margin-right: 2px; }
  .lc-chip { height: 24px; padding: 0 6px 0 9px; border-radius: var(--r-md); background: var(--raised); border: 1px solid transparent; color: var(--text-soft); font-size: 12.5px; display: inline-flex; align-items: center; gap: 5px; max-width: 100%; transition: border-color 0.2s var(--ease), color 0.2s var(--ease); }
  .lc-chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lc-chip :global(svg.i) { width: 12px; height: 12px; flex: none; color: var(--muted); }
  .lc-chip:hover { border-color: var(--line2); color: var(--text); }
  .lc-clear { height: 24px; padding: 0 6px; border-radius: var(--r-sm); color: var(--muted); font-size: 12.5px; transition: color 0.2s var(--ease), background 0.2s var(--ease); }
  .lc-clear:hover { color: var(--text); background: var(--raised); }
  .lc-bulk { position: sticky; top: 10px; z-index: 25; display: grid; gap: 10px; padding: 12px 14px; border: 1px solid var(--mint-line); border-radius: var(--r-xl); background: var(--surface); box-shadow: var(--shadow-pop); }
  .lc-bulk strong { font-weight: 600; font-size: 13.5px; }

  .lc-grid { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 18px 14px; }
  .lc-cell { position: relative; min-width: 0; }
  .lc-cell.lc-up { z-index: 20; }
  .lc-tile { width: 100%; text-align: left; display: grid; align-content: start; gap: 9px; padding: 0; border-radius: var(--r-xl); transition: transform 0.15s var(--ease); }
  .lc-tile:active { transform: scale(0.98); }
  .lc-tile:focus-visible { outline: none; }
  .lc-cover { position: relative; display: block; aspect-ratio: 4 / 3; border-radius: var(--r-xl); overflow: hidden; background: var(--raised); box-shadow: 0 0 0 1px var(--line2); transition: box-shadow 0.25s var(--ease); }
  .lc-tile:hover .lc-cover { box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.24); }
  .lc-tile.open .lc-cover, .lc-tile.picked .lc-cover { box-shadow: 0 0 0 2px var(--mint); }
  .lc-tile:focus-visible .lc-cover { box-shadow: 0 0 0 2px var(--mint), 0 0 0 5px var(--mint-dim); }
  .lc-status { position: absolute; top: 8px; left: 8px; z-index: 1; height: 20px; padding: 0 7px; border-radius: var(--r-sm); background: rgba(10, 10, 11, 0.78); border: 1px solid var(--line2); color: var(--muted); font-size: 11px; font-weight: 500; display: inline-flex; align-items: center; gap: 6px; transition: opacity 0.2s var(--ease); }
  .lc-status.ok { color: var(--mint); }
  .lc-status.draft { color: var(--amber); }
  /* hovering (or keyboard focus) fades the description in over the cover, as the workflow picker does */
  .lc-about { position: absolute; inset: 0; z-index: 2; display: flex; flex-direction: column; justify-content: flex-end; gap: 6px; padding: 10px 11px; background: linear-gradient(180deg, rgba(10, 10, 11, 0.35) 0%, rgba(10, 10, 11, 0.88) 55%, rgba(10, 10, 11, 0.94) 100%); opacity: 0; transition: opacity 0.25s var(--ease); pointer-events: none; }
  .lc-about-text { font-size: 12.5px; line-height: 1.45; color: rgba(255, 255, 255, 0.92); display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 6; line-clamp: 6; overflow: hidden; text-wrap: pretty; overflow-wrap: anywhere; }
  .lc-about-tags { font-size: 11.5px; color: var(--mint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lc-tile:hover .lc-about, .lc-tile:focus-visible .lc-about { opacity: 1; }
  .lc-tile:hover .lc-status, .lc-tile:focus-visible .lc-status { opacity: 0; }
  .lc-meta { display: grid; gap: 3px; min-width: 0; padding: 0 2px; }
  .lc-name { font-size: 13px; font-weight: 500; line-height: 1.35; color: var(--text-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: color 0.2s var(--ease); }
  .lc-tile:hover .lc-name, .lc-tile.open .lc-name, .lc-tile.picked .lc-name { color: var(--text); }
  .lc-line { font-size: 12px; line-height: 1.45; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lc-line.warn { color: var(--amber); }

  .lc-mark { position: absolute; top: 8px; right: 8px; z-index: 3; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; background: rgba(10, 10, 11, 0.78); border: 1px solid var(--line2); color: transparent; pointer-events: none; transition: background 0.2s var(--ease), color 0.2s var(--ease); }
  .lc-mark.on { background: var(--mint); border-color: var(--mint); color: var(--mint-ink); }
  .lc-mark :global(svg.i) { width: 13px; height: 13px; stroke-width: 2.6; }
  .lc-tagbtn { position: absolute; top: 6px; right: 6px; z-index: 3; min-width: 26px; height: 26px; padding: 0 6px; border-radius: 13px; display: inline-flex; align-items: center; justify-content: center; gap: 4px; background: rgba(10, 10, 11, 0.78); border: 1px solid var(--line2); color: var(--text-soft); font-size: 11.5px; opacity: 0; transition: opacity 0.2s var(--ease), color 0.2s var(--ease), border-color 0.2s var(--ease); }
  .lc-delbtn { position: absolute; top: 38px; right: 6px; z-index: 3; width: 26px; height: 26px; border-radius: 13px; display: grid; place-items: center; background: rgba(10, 10, 11, 0.78); border: 1px solid var(--line2); color: var(--text-soft); opacity: 0; transition: opacity 0.2s var(--ease), color 0.2s var(--ease), border-color 0.2s var(--ease), background 0.2s var(--ease); }
  .lc-delbtn :global(svg.i) { width: 13px; height: 13px; }
  .lc-cell:hover .lc-delbtn, .lc-delbtn:focus-visible { opacity: 1; }
  .lc-delbtn:hover, .lc-delbtn:focus-visible { border-color: var(--red-line); color: var(--red); background: var(--red-dim); }
  @media (hover: none) { .lc-delbtn { opacity: 1; } }
  .lc-tagbtn :global(svg.i) { width: 13px; height: 13px; }
  .lc-tagbtn.has { color: var(--mint); }
  .lc-cell:hover .lc-tagbtn, .lc-tagbtn:focus-visible, .lc-tagbtn[aria-expanded='true'] { opacity: 1; }
  .lc-tagbtn:hover { border-color: var(--mint-line); color: var(--text); }
  @media (hover: none) { .lc-tagbtn { opacity: 1; } }
  .lc-pop { position: absolute; z-index: 30; left: 0; top: 38px; width: 264px; padding: 12px; border-radius: var(--r-xl); background: var(--float); border: 1px solid var(--line2); box-shadow: var(--shadow-pop); }
  .lc-pop.right { left: auto; right: 0; }

</style>
