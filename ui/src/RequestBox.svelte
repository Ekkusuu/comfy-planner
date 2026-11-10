<script>
  // The request box: choose the workflow, say what you want in plain words, the local LLM plans the jobs on that workflow.
  // Ctrl+Enter plans. The workflow is always the user's choice; the last one is remembered.
  // The unsent text and input images are kept in localStorage, so they survive a tab change and a restart.
  // Typing @ offers the LoRAs the chosen workflow can use; a LoRA named in the text is always used.
  import { onMount, tick } from 'svelte'
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import LoraCover from './LoraCover.svelte'
  import WorkflowPicker from './WorkflowPicker.svelte'
  import { api } from './api.js'
  import { live, previews, toastError, getWorkflow, refreshLoras } from './store.svelte.js'
  import { autogrow } from './motion.js'
  import { mentioned, pretty, scan, stem, typing, withoutMention } from './mentions.js'

  // onplan(request, workflowId, inputImages) -> Promise<boolean>: true when the plan was accepted (the box is cleared then).
  // `planning`: a plan is already running; the box stays open, and a new request joins the line behind it.
  let { planning = false, primary = true, onplan } = $props()

  const KEY = 'cp.plan.workflow'
  const DRAFT = 'cp.plan.draft'
  function savedDraft() {
    try {
      const d = JSON.parse(localStorage.getItem(DRAFT))
      return { text: typeof d?.text === 'string' ? d.text : '', images: Array.isArray(d?.images) ? d.images.filter((n) => typeof n === 'string') : [] }
    } catch {
      return { text: '', images: [] }
    }
  }
  const draft = savedDraft()
  let text = $state(draft.text)
  let images = $state(draft.images) // names on ComfyUI, from POST /api/comfy/upload
  let uploading = $state(0)
  let area
  let picker
  let chosen = $state(localStorage.getItem(KEY))
  let open = $state(!localStorage.getItem(KEY)) // the tiles stay open until a workflow is chosen
  let needed = $state(0) // input images the chosen workflow needs (0 unless it edits pictures)
  let broken = $state(false) // the chosen workflow's cover failed to load

  // workflows the planner can use: runnable, with an approved card (or a draft one when drafts are allowed)
  const cardOk = (c) => c.status === 'approved' || (live.config.allow_draft_cards && c.status === 'draft')
  const usable = $derived(
    live.workflows
      .map((w) => ({ w, c: live.workflowCards.find((c) => c.key === w.id) }))
      .filter(({ w, c }) => w.runnable && c && cardOk(c))
      .map(({ w, c }) => ({ ...w, best_for: c.best_for, purpose: c.purpose })) // what it is for, shown on the tiles
  )
  const skipped = $derived(live.workflows.length - usable.length)
  const current = $derived(usable.find((w) => w.id === chosen) ?? null)
  const noCards = $derived(live.ready && usable.length === 0)
  const edits = $derived(current?.kind === 'edit')
  const missingImages = $derived(edits && images.length < 1) // the first image is required, more are optional
  const canPlan = $derived(!!current && text.trim().length > 0 && uploading === 0 && !missingImages)

  $effect(() => {
    // a remembered workflow that can't be used any more is dropped once the lists have loaded
    if (live.ready && chosen && !current) {
      chosen = null
      open = true
    }
  })
  $effect(() => {
    const id = current?.kind === 'edit' ? current.id : null
    needed = 0
    broken = false
    if (id) getWorkflow(id).then((w) => { if (current?.id === id) needed = w.slots?.input_images?.length ?? 1 }).catch(toastError)
    else if (current) images = [] // not before the workflows have loaded: a saved draft keeps its images until then
  })
  $effect(() => {
    // the draft follows the box; an empty box leaves nothing behind
    const saved = { text, images: [...images] }
    try {
      if (saved.text.trim() || saved.images.length) localStorage.setItem(DRAFT, JSON.stringify(saved))
      else localStorage.removeItem(DRAFT)
    } catch {}
  })
  onMount(() => {
    if (!live.loras.length) refreshLoras()
    requestAnimationFrame(regrow) // a restored long text gets its height once the box is laid out
  })

  async function choose(id) {
    chosen = id
    localStorage.setItem(KEY, id)
    await new Promise((r) => setTimeout(r, 380)) // let the tick land before the tiles fold away
    open = false
    await tick()
    if (!text.trim()) area?.focus({ preventScroll: true })
  }

  function regrow() {
    area?.dispatchEvent(new Event('input')) // the autogrow action listens for it
  }
  async function submit() {
    if (!canPlan) return
    const request = text.trim()
    const sent = [...images]
    if (await onplan(request, current.id, sent)) {
      text = ''
      images = []
      requestAnimationFrame(regrow)
    }
  }
  /** Put a request back after it failed, unless something new has been typed meanwhile. */
  export function restore(request, sent = []) {
    if (!text.trim()) {
      text = request
      images = sent
      requestAnimationFrame(regrow)
    }
  }

  // ----- @: pick a LoRA by name -----
  const family = $derived(live.workflowCards.find((c) => c.key === chosen)?.family ?? null)
  const loraKeys = $derived(live.loras.filter((l) => l.card).map((l) => l.key))
  const loraCardOk = (l) => l.status === 'approved' || (live.config.allow_draft_cards && l.status === 'draft')
  const takes = (l) => !!l.card && l.state !== 'removed' && loraCardOk(l) && l.card.family === family // the planner would use it
  const named = $derived(mentioned(text, loraKeys))
  let typed = $state(null) // { at, query }: the word after an @ at the caret
  let active = $state(0)
  const rows = $derived.by(() => {
    if (!typed || !family) return []
    const words = typed.query.toLowerCase().split(/\s+/).filter(Boolean)
    const haystack = (l) => [l.key, ...(l.card.trigger_words ?? []), ...(l.user_tags ?? [])].join(' ').toLowerCase()
    const starts = (l) => (words.length && stem(l.key).toLowerCase().startsWith(words[0]) ? 0 : 1)
    return live.loras
      .filter((l) => takes(l) && !named.includes(l.key) && words.every((w) => haystack(l).includes(w)))
      .sort((a, b) => starts(a) - starts(b) || stem(a.key).localeCompare(stem(b.key), undefined, { sensitivity: 'base', numeric: true }))
  })
  const menu = $derived(rows.length > 0) // with nothing to offer it stays closed: an @ is also how artist tags are written
  const chips = $derived(named.map((key) => {
    const l = live.loras.find((x) => x.key === key)
    return { key, entry: l, ok: takes(l), why: !loraCardOk(l) ? 'its card is not approved' : l.card.family !== family ? `it is a ${l.card.family} LoRA` : 'it is not available' }
  }))

  function look() {
    // is the caret in a word after an @ that is not yet a whole name?
    const caret = area.selectionStart
    const t = area.selectionEnd === caret ? typing(area.value, caret) : null
    typed = t && !scan(area.value, loraKeys).some((m) => m.start === t.at && m.end <= caret) ? t : null
    active = 0
  }
  async function insert(l) {
    const caret = area.selectionStart
    const before = area.value.slice(0, typed.at)
    const after = area.value.slice(caret)
    const name = '@' + stem(l.key)
    text = before + name + (/^\s/.test(after) ? '' : ' ') + after
    typed = null
    await tick()
    area.focus({ preventScroll: true })
    area.setSelectionRange(before.length + name.length + 1, before.length + name.length + 1)
    regrow()
  }
  function unname(key) {
    text = withoutMention(text, key, loraKeys)
    requestAnimationFrame(regrow)
  }
  $effect(() => {
    if (menu) document.getElementById(`rq-at-${active}`)?.scrollIntoView({ block: 'nearest' })
  })
  function key(e) {
    if (e.isComposing) return
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      typed = null
      submit()
    } else if (menu && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault()
      active = (active + (e.key === 'ArrowDown' ? 1 : rows.length - 1)) % rows.length
    } else if (menu && (e.key === 'Enter' || e.key === 'Tab') && !e.shiftKey && !e.altKey) {
      e.preventDefault()
      insert(rows[active])
    } else if (menu && e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      typed = null
    }
  }

  async function addFiles(files) {
    for (const file of files) {
      if (!file.type.startsWith('image/')) {
        toastError(new Error(`${file.name} is not an image.`))
        continue
      }
      uploading++
      try {
        const { name } = await api.upload(file)
        previews[name] = URL.createObjectURL(file)
        images.push(name)
      } catch (e) {
        toastError(e)
      } finally {
        uploading--
      }
    }
  }
  const shown = (name) => name.replace(/^cp_[0-9a-f]{8}_/, '')
</script>

<div class="rq">
  {#if usable.length}
    <div class="wf">
      <div class="wf-head">
        {#if current}
          <button type="button" class="sel" class:open aria-expanded={open} aria-controls="rq-tiles" title={open ? 'Close the list' : 'Change the workflow'} onclick={() => (open = !open)}>
            <span class="mini">
              {#if current.cover_image_id !== null && !broken}
                <img src={api.thumbUrl(current.cover_image_id, 384)} alt="" decoding="async" onerror={() => (broken = true)} />
              {:else}
                <Icon name={edits ? 'wand' : 'image'} />
              {/if}
            </span>
            <span class="nm-wf">{current.title}</span>
            <span class="chev"><Icon name="chevron" /></span>
          </button>
        {:else}
          <span class="ask">Choose a workflow</span>
        {/if}
        <InfoTip title="Workflow" text="You choose the workflow; the LLM never does. It plans the LoRAs, settings and prompts for this workflow only, and splits your request into several jobs if you ask for different things. The workflow's card in the Library tells it how to prompt." label="About the workflow" />
      </div>
      <div class="reveal" class:open id="rq-tiles">
        <div>
          <div class="tiles">
            <WorkflowPicker workflows={usable} value={chosen} onchange={choose} />
            {#if skipped}
              <p class="skipped">{skipped} {skipped === 1 ? 'workflow isn’t' : 'workflows aren’t'} listed: {skipped === 1 ? 'it is' : 'they are'} not runnable or {skipped === 1 ? 'has' : 'have'} no approved card. <a href="#/library">See why in Library</a></p>
            {/if}
          </div>
        </div>
      </div>
    </div>
  {/if}

  <div class="rq-input">
    <textarea
      bind:this={area}
      bind:value={text}
      use:autogrow
      onkeydown={key}
      oninput={look}
      onclick={look}
      onkeyup={(e) => { if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) look() }}
      onblur={() => (typed = null)}
      rows="2"
      aria-label="Describe what you want"
      aria-haspopup="listbox"
      aria-controls="rq-at"
      aria-activedescendant={menu ? `rq-at-${active}` : undefined}
      placeholder={edits ? 'Describe the change, for example: make the coat red and add rain.\nSplit images with ---, share a rule with ===, type @ to pick a LoRA.' : 'Describe what you want, for example: four moody portraits in the rain, and two cozy cabin interiors.\nSplit images with ---, share a rule with ===, type @ to pick a LoRA.'}
    ></textarea>
    {#if menu}
      <div class="at-menu" id="rq-at" role="listbox" aria-label="LoRAs">
        {#each rows as l, i (l.key)}
          <button type="button" class="at-row" class:on={i === active} id="rq-at-{i}" role="option" aria-selected={i === active} tabindex="-1" title={l.key}
            onmousedown={(e) => e.preventDefault()} onmousemove={() => (active = i)} onclick={() => insert(l)}>
            <span class="at-thumb"><LoraCover entry={l} /></span>
            <span class="at-name">{pretty(l.key)}</span>
            <span class="at-kind">{l.card.kind}</span>
          </button>
        {/each}
      </div>
    {/if}
  </div>

  {#if chips.length}
    <div class="named" aria-label="LoRAs named in the request">
      {#each chips as c (c.key)}
        <span class="at-chip" class:off={!c.ok} title={c.ok ? `${c.key} is used at its recommended weight` : `${c.key} is not used: ${c.why}`}>
          <span class="at-thumb"><LoraCover entry={c.entry} /></span>
          <span class="at-nm">{pretty(c.key)}</span>
          {#if !c.ok}<span class="at-off">not used</span>{/if}
          <button type="button" class="at-x" aria-label="Remove {pretty(c.key)} from the request" onclick={() => unname(c.key)}><Icon name="x" /></button>
        </span>
      {/each}
    </div>
  {/if}

  {#if images.length || uploading}
    <div class="imgs">
      {#each images as name (name)}
        <span class="img">
          {#if previews[name]}<img src={previews[name]} alt="" />{:else}<Icon name="image" />{/if}
          <span class="nm">{shown(name)}</span>
          <button type="button" class="x" aria-label="Remove {shown(name)}" onclick={() => (images = images.filter((n) => n !== name))}><Icon name="x" /></button>
        </span>
      {/each}
      {#if uploading}<span class="img up"><span class="spin" style="display:grid"><Icon name="loader" /></span><span class="nm">Uploading</span></span>{/if}
    </div>
  {/if}

  <div class="bar">
    {#if edits}
      <button type="button" class="btn sm" onclick={() => picker.click()}><Icon name="image" />Add image</button>
      <InfoTip title="Input images" text={needed > 1 ? `This workflow edits pictures instead of starting from nothing. Add 1 to ${needed} images: the first is the one it edits, the others are optional (for example clothing to put on the person).` : 'This workflow edits pictures instead of starting from nothing. Add the image it should work from.'} label="About input images" />
      <input bind:this={picker} type="file" accept="image/*" multiple class="sr" tabindex="-1" onchange={(e) => { addFiles([...e.currentTarget.files]); e.currentTarget.value = '' }} />
    {/if}
    <span class="spacer"></span>
    {#if usable.length && !current}
      <span class="need-wf">Choose a workflow to plan with</span>
    {:else if missingImages}
      <span class="need-wf">Add the image to edit</span>
    {:else}
      <span class="keys">Ctrl+Enter</span>
    {/if}
    <button type="button" class="btn" class:primary onclick={submit} disabled={!canPlan}>
      <Icon name="spark" />{planning ? 'Queue plan' : 'Plan'}
    </button>
    <InfoTip title="Plan" text="The local LLM splits your request into jobs on the workflow you chose, picks LoRAs and settings for each, writes a different prompt per image, and says why. Nothing runs until you press Run. You can plan another request while one is running: it waits in line and is planned next. Ask for as many images as you like: 100 different scenes are planned one by one, each on its own, which takes a while. To keep two things apart, put --- between them (inline or on its own line); each part is planned as a request of its own. For something that applies to all of them, write it first and end it with ===, for example: for all the images use the ink LoRA === a knight --- a cabin --- 5 dragons. Type @ to pick a LoRA by name: a LoRA named that way is always used, at its recommended weight, on the images the text applies to." label="About planning" />
  </div>

  {#if noCards}
    <p class="warn">
      <Icon name="alert" />
      <span>Planning needs a runnable workflow with an approved card, and none is ready yet. <a href="#/library">Approve one in the Library</a>, or <a href="#/settings">allow draft cards in Settings</a>.</span>
    </p>
  {/if}
</div>

<style>
  .rq { background: var(--surface); border: 1px solid var(--line2); border-radius: var(--r-2xl); padding: 12px 18px 12px; display: grid; gap: 10px; transition: border-color 0.25s var(--ease), box-shadow 0.25s var(--ease); }
  .rq:focus-within { border-color: var(--mint-line); box-shadow: 0 0 0 4px var(--mint-dim); }
  /* the picker's scroll arrows sit in the header row: positioned against .wf they are not clipped by the folding tiles */
  .wf { display: grid; position: relative; }
  .tiles :global(.picker) { position: static; }
  .tiles :global(.nav) { top: 2px; right: -6px; }
  .wf-head { display: flex; align-items: center; gap: 4px; min-height: 36px; min-width: 0; padding-right: 64px; }
  .ask { font-size: 13.5px; font-weight: 500; color: var(--text-soft); margin-right: 2px; }
  .sel { display: inline-flex; align-items: center; gap: 9px; min-width: 0; height: 36px; margin-left: -4px; padding: 0 8px 0 4px; border-radius: var(--r-lg); color: var(--text); transition: background 0.2s var(--ease); }
  .sel:hover { background: var(--raised); }
  .sel:focus-visible { outline: 2px solid var(--mint); outline-offset: 1px; }
  .mini { width: 32px; height: 26px; border-radius: 6px; overflow: hidden; background: var(--raised); box-shadow: 0 0 0 1px var(--line2); display: grid; place-items: center; flex: none; color: var(--faint); }
  .mini img { width: 100%; height: 100%; object-fit: cover; }
  .mini :global(svg.i) { width: 14px; height: 14px; }
  .nm-wf { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13.5px; font-weight: 500; }
  .chev { display: grid; color: var(--faint); transition: transform 0.35s var(--ease), color 0.2s var(--ease); }
  .sel:hover .chev { color: var(--text-soft); }
  .sel.open .chev { transform: rotate(180deg); }
  .chev :global(svg.i) { width: 14px; height: 14px; }
  .tiles { display: grid; gap: 6px; padding: 2px 0 4px; }
  .skipped { margin: 0; font-size: 12.5px; line-height: 1.45; color: var(--muted); }
  .skipped a { color: var(--text-soft); text-decoration: underline; text-decoration-color: var(--line2); text-underline-offset: 3px; transition: text-decoration-color 0.2s var(--ease); }
  .skipped a:hover { text-decoration-color: var(--text-soft); }
  .rq-input { position: relative; }
  /* the menu hangs under the textarea and over what follows it (chips, the bar); .rq doesn't clip */
  .at-menu { position: absolute; z-index: 30; left: -8px; right: -8px; top: calc(100% + 6px); max-height: 264px; overflow-y: auto; overscroll-behavior: contain; padding: 4px; display: grid; gap: 1px; background: var(--float); border: 1px solid var(--line2); border-radius: var(--r-xl); box-shadow: var(--shadow-pop); animation: fadein 0.18s var(--ease) backwards; }
  .at-row { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 42px; padding: 4px 10px 4px 4px; border-radius: var(--r-lg); text-align: left; color: var(--text-soft); font-size: 13.5px; }
  .at-row.on { background: var(--raised); color: var(--text); box-shadow: inset 0 0 0 1px var(--mint-line); }
  .at-thumb { position: relative; width: 34px; height: 34px; border-radius: 6px; overflow: hidden; flex: none; background: var(--raised); box-shadow: 0 0 0 1px var(--line2); }
  .at-name { min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .at-kind { flex: none; color: var(--faint); font-size: 12px; text-transform: capitalize; }
  .named { display: flex; flex-wrap: wrap; gap: 6px; }
  .at-chip { display: inline-flex; align-items: center; gap: 8px; height: 34px; padding: 3px 4px 3px 3px; border-radius: var(--r-md); background: var(--raised); color: var(--muted); font-size: 12.5px; max-width: 280px; animation: fadein 0.35s var(--ease) backwards; }
  .at-chip .at-thumb { width: 28px; height: 28px; border-radius: 5px; box-shadow: none; }
  .at-nm { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-soft); }
  .at-chip.off .at-nm { color: var(--muted); text-decoration: line-through; text-decoration-color: var(--line2); }
  .at-off { flex: none; color: var(--amber); font-size: 12px; }
  .at-x { width: 24px; height: 24px; border-radius: 5px; display: grid; place-items: center; flex: none; color: var(--faint); transition: color 0.2s var(--ease), background 0.2s var(--ease); }
  .at-x:hover { color: var(--text); background: rgba(255, 255, 255, 0.07); }
  .at-x :global(svg.i) { width: 12px; height: 12px; }
  textarea { display: block; width: 100%; min-height: 56px; max-height: 240px; overflow-y: auto; resize: none; background: transparent; border: 0; outline: none; padding: 0; margin: 0; color: var(--text); font: inherit; font-size: 15px; line-height: 1.6; caret-color: var(--mint); }
  textarea::placeholder { color: var(--faint); }
  .bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding-top: 10px; border-top: 1px solid var(--line); }
  .keys { color: var(--faint); font-family: var(--mono); font-size: 12px; margin-right: 2px; }
  .need-wf { color: var(--muted); font-size: 12.5px; margin-right: 2px; }
  .imgs { display: flex; flex-wrap: wrap; gap: 6px; }
  .img { display: inline-flex; align-items: center; gap: 8px; height: 34px; padding: 3px 4px 3px 3px; border-radius: var(--r-md); background: var(--raised); color: var(--muted); font-size: 12.5px; max-width: 240px; animation: fadein 0.35s var(--ease) backwards; }
  .img img { width: 28px; height: 28px; border-radius: 5px; object-fit: cover; flex: none; }
  .img > :global(svg.i) { margin: 0 6px; }
  .nm { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-soft); }
  .img.up { padding-right: 10px; }
  .x { width: 24px; height: 24px; border-radius: 5px; display: grid; place-items: center; color: var(--faint); transition: color 0.2s var(--ease), background 0.2s var(--ease); }
  .x:hover { color: var(--text); background: rgba(255, 255, 255, 0.07); }
  .x :global(svg.i) { width: 12px; height: 12px; }
  .warn { display: flex; align-items: flex-start; gap: 8px; margin: 0; padding: 9px 11px; border-radius: var(--r-md); background: var(--amber-dim); border: 1px solid var(--amber-line); color: var(--text-soft); font-size: 13px; line-height: 1.45; }
  .warn :global(svg.i) { color: var(--amber); margin-top: 1px; }
  .warn a { color: var(--text); border-bottom: 1px solid var(--amber-line); }
  .warn a:hover { border-color: var(--amber); }
</style>
