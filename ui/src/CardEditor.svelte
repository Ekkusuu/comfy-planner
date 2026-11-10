<script>
  // The workflow card: build or re-generate it, review the diff, edit the fields, approve.
  import { untrack } from 'svelte'
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import CardLoadFailed from './CardLoadFailed.svelte'
  import CardDiff from './CardDiff.svelte'
  import ListField from './ListField.svelte'
  import RangeField from './RangeField.svelte'
  import { api } from './api.js'
  import { wheelScroll } from './wheelScroll.js'
  import { live, toast, toastError, refreshWorkflowCards } from './store.svelte.js'
  import { isBuilding, buildText, clone, same, slotRows } from './cards.js'

  let { wf } = $props() // list entry: { id, title, ... }

  // Everything below depends on the workflow's id only, not on the list entry, which is replaced on every refresh.
  const id = $derived(wf.id)
  let data = $state(null) // GET /api/cards/workflows/<id>
  let draft = $state(null) // the card being edited
  let busy = $state(false)
  let loadError = $state(null) // the message when the card could not be loaded
  let loads = 0 // counts loads: only the latest one may fill the editor

  const build = $derived(live.cardBuilds[`workflow:${wf.id}`])
  const building = $derived(isBuilding(build))
  const dirty = $derived(!!draft && !!data?.card && !same(draft, data.card))
  const hasCard = $derived(!!data?.card)
  const hasHires = $derived(!!draft?.settings?.hires)
  const sizeLine = { format: (z) => `${z.w}x${z.h}${z.label ? ' ' + z.label : ''}`, parse: (l) => { const m = l.match(/^(\d+)\s*[x×]\s*(\d+)\s*(.*)$/i); return m ? { w: +m[1], h: +m[2], label: m[3].trim() } : null } }

  async function load(which = id) {
    const mine = ++loads
    loadError = null
    try {
      const out = await api.card('workflows', which)
      if (mine !== loads) return // another workflow was opened, or a newer load started
      data = out
      draft = out.card ? clone(out.card) : null
    } catch (e) {
      if (mine === loads) loadError = e?.message || String(e)
    }
  }
  const retry = () => {
    data = draft = null
    load()
  }
  $effect(() => {
    const which = id
    untrack(() => {
      data = draft = null
      load(which)
    })
  })
  // a build for this card finished: show what it wrote
  let lastStage = untrack(() => build?.stage) // a build that finished before the editor opened is already what load() shows
  $effect(() => {
    const stage = build?.stage
    if (stage !== lastStage) {
      lastStage = stage
      if (stage === 'done' || stage === 'failed') load()
    }
  })

  async function run(fn, done) {
    busy = true
    try {
      const out = await fn()
      if (out && out.kind) {
        data = out
        draft = out.card ? clone(out.card) : null
        refreshWorkflowCards() // the Library list and the request box show the card's status too
      }
      if (done) toast(done)
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
  async function startBuild() {
    try {
      live.cardBuilds[`workflow:${wf.id}`] = await api.buildCard('workflows', wf.id)
    } catch (e) {
      toastError(e)
    }
  }
  const save = () => run(() => api.saveCard('workflows', wf.id, draft), 'Saved')
  const approve = () => run(async () => {
    if (dirty) await api.saveCard('workflows', wf.id, draft)
    return api.approveCard('workflows', wf.id)
  }, 'Approved')
  const accept = () => run(() => api.acceptCard('workflows', wf.id), 'Using the re-generated card')
  const reject = () => run(() => api.rejectCard('workflows', wf.id))
  const discard = () => (draft = clone(data.card))
  const addExample = () => (draft.examples = [...draft.examples, ''])
  const dropExample = (i) => (draft.examples = draft.examples.filter((_, n) => n !== i))
  const grow = (node) => {
    const fit = () => { node.style.height = 'auto'; node.style.height = node.scrollHeight + 2 + 'px' }
    fit(); node.addEventListener('input', fit)
    return { destroy: () => node.removeEventListener('input', fit) }
  }
</script>

<svelte:boundary onerror={(e) => console.error(e)}>
{#if loadError}
  <CardLoadFailed message={loadError} onretry={retry} />
{:else if !data}
  <p class="muted">Loading the card…</p>
{:else}
  <div class="bar">
    {#if hasCard}
      <span class="badge" class:draft={data.status === 'draft'} class:ok={data.status === 'approved'}>{data.status === 'approved' ? 'Approved' : 'Draft'}</span>
      <InfoTip text="A draft is the LLM's first attempt, so the planner ignores it until you approve it (unless Settings allows drafts). Check the fields below, then approve." label="Draft or approved?" />
    {:else}
      <span class="badge">No card</span>
    {/if}
    {#if building}
      <span class="status"><span class="spin" style="display:grid"><Icon name="loader" /></span>{buildText(build)}</span>
    {:else if build?.stage === 'failed'}
      <span class="status err"><Icon name="alert" />{build.message}</span>
    {/if}
    <span class="spacer"></span>
    <button class="btn" onclick={startBuild} disabled={building || busy} class:primary={!hasCard}>
      {#if hasCard}<Icon name="retry" />Re-generate{:else}<Icon name="spark" />Build card{/if}
    </button>
    {#if hasCard}
      <button class="btn" class:primary={data.status === 'draft' && !data.pending} onclick={approve} disabled={busy || data.status === 'approved'}>
        <Icon name="check" />{data.status === 'approved' ? 'Approved' : 'Approve'}
      </button>
    {/if}
    <InfoTip text="Build card: the LLM reads this workflow and writes a draft. Re-generate: it writes a fresh one next to your current card so you can compare." label="Build and re-generate" />
  </div>

  {#if !hasCard}
    <div class="empty compact">
      <h3>No card yet</h3>
      <p>The LLM reads this workflow’s notes and settings and writes a draft. You review it here before the planner uses it.</p>
    </div>
  {:else}
    {#if data.pending}
      <section class="cf-sec pending">
        <div class="bar">
          <h4 class="hx">A re-generated card is ready<InfoTip text="Compare it with your current card. Keep current changes nothing; Use re-generated replaces it, and the card goes back to draft until you approve it." label="About re-generated cards" /></h4>
          <span class="spacer"></span>
          <button class="btn" onclick={reject} disabled={busy}>Keep current</button>
          <button class="btn primary" onclick={accept} disabled={busy}>Use re-generated</button>
        </div>
        <CardDiff before={data.card} after={data.pending} />
        {#if data.pending.needs_attention.length}<p class="attn"><Icon name="alert" />The LLM couldn’t write: {data.pending.needs_attention.join(', ')}.</p>{/if}
      </section>
    {/if}
    {#if draft.needs_attention.length}
      <p class="attn"><Icon name="alert" />The LLM couldn’t write: {draft.needs_attention.join(', ')}. Fill them in, or re-generate.</p>
    {/if}

    <div class="cf">
      <section class="cf-sec">
        <div class="cf-grid">
          <span class="lb"><label for="c-purpose">Purpose</label><InfoTip text="What this workflow is for, in a sentence. The planner reads it to decide when to use the workflow." /></span>
          <textarea id="c-purpose" class="textarea" rows="2" bind:value={draft.purpose} use:grow></textarea>
          <span class="lb"><label for="c-best">Best for</label><InfoTip text="The kinds of images it does best. Tells the planner what this workflow is for, so the prompts and settings suit it." /></span>
          <input id="c-best" class="field" bind:value={draft.best_for} />
          <span class="lb"><label for="c-family">Family</label><InfoTip text="The kind of base model the workflow uses. A LoRA only works with workflows of the same family." /></span>
          <select id="c-family" class="select" style="max-width:220px" bind:value={draft.family}>
            {#each live.config.card_families as f}<option value={f}>{f}</option>{/each}
          </select>
        </div>
      </section>

      <section class="cf-sec">
        <h4 class="hx">Prompt style<InfoTip text="How the prompt writer should phrase prompts for this workflow." /></h4>
        <div class="cf-grid">
          <span class="lb">Kind<InfoTip text="How prompts should be written: short comma-separated tags, full sentences, or a mix." /></span>
          <div class="seg" role="radiogroup" aria-label="Prompt kind" style="justify-self:start">
            {#each [['tags', 'Tags'], ['sentences', 'Sentences'], ['mixed', 'Mixed']] as [k, label]}
              <button role="radio" aria-checked={draft.prompt_style.kind === k} onclick={() => (draft.prompt_style.kind = k)}>{label}</button>
            {/each}
          </div>
          <span class="lb"><label for="c-prefix">Quality prefix</label><InfoTip text="Tags every prompt for this workflow starts with, like “masterpiece, best quality”." /></span>
          <input id="c-prefix" class="field mono" bind:value={draft.prompt_style.quality_prefix} placeholder="Tags every prompt starts with" />
          <span class="lb"><label for="c-rules">Rules</label><InfoTip text="House rules for the prompt writer on this workflow, one per line." /></span>
          <ListField id="c-rules" bind:value={draft.prompt_style.rules} rows={4} placeholder="One rule per line" />
        </div>
      </section>

      <section class="cf-sec">
        <h4 class="hx">Settings<InfoTip text="Ranges the planner stays inside when it picks values for this workflow." /></h4>
        <div class="cf-grid">
          <span class="lb">CFG<InfoTip text="How strictly the image follows the prompt. Recommended is the starting value; min and max are the safe range." /></span><RangeField bind:value={draft.settings.cfg} />
          <span class="lb">Steps<InfoTip text="How many refinement passes. More is slower, with diminishing returns." /></span><RangeField bind:value={draft.settings.steps} />
          {#if hasHires}
            <span class="lb">Hires steps<InfoTip text="Passes in the second, larger redraw." /></span><RangeField bind:value={draft.settings.hires.steps} />
            <span class="lb">Hires denoise<InfoTip text="How much the hires pass may change the image, from 0 to 1. Low stays closer to the first pass." /></span><RangeField bind:value={draft.settings.hires.denoise} />
          {/if}
          <span class="lb"><label for="c-samplers">Samplers</label><InfoTip text="The sampling methods that work well here. The planner picks from this list." /></span>
          <ListField id="c-samplers" mode="commas" bind:value={draft.settings.samplers} mono placeholder="euler, er_sde" />
          <span class="lb"><label for="c-sched">Schedulers</label><InfoTip text="How noise is removed across the steps. The planner picks from this list." /></span>
          <ListField id="c-sched" mode="commas" bind:value={draft.settings.schedulers} mono placeholder="simple" />
          <span class="lb"><label for="c-sizes">Sizes</label><InfoTip text="Image sizes this workflow suits, one per line, with an optional name." /></span>
          <ListField id="c-sizes" bind:value={draft.settings.sizes} rows={2} mono format={sizeLine.format} parse={sizeLine.parse} placeholder="1024x1536 portrait" />
          <span class="lb">Pixel budget<InfoTip text="The smallest and largest total pixels (width × height) the model handles well." /></span>
          <div class="range">
            <label><span>Min</span><input class="field num" type="number" bind:value={draft.settings.pixel_budget.min} /></label>
            <label><span>Max</span><input class="field num" type="number" bind:value={draft.settings.pixel_budget.max} /></label>
          </div>
        </div>
      </section>

      <section class="cf-sec">
        <h4 class="hx">Warnings<InfoTip text="Things the planner must not break, like “CFG must stay at 1”." /></h4>
        <ListField bind:value={draft.warnings} rows={2} placeholder="One warning per line, for example “CFG must stay at 1”" />
      </section>

      <section class="cf-sec">
        <div class="bar"><h4 class="hx">Examples<InfoTip text="Sample prompts that show what works here. The prompt writer copies their style." /></h4><span class="spacer"></span><button class="btn sm" onclick={addExample}><Icon name="plus" />Add</button></div>
        {#each draft.examples as _, i}
          <div class="ex">
            <textarea class="textarea" rows="2" bind:value={draft.examples[i]} use:grow aria-label="Example {i + 1}"></textarea>
            <button class="icon-btn sm" onclick={() => dropExample(i)} aria-label="Remove example {i + 1}"><Icon name="x" /></button>
          </div>
        {/each}
      </section>

      <section class="cf-sec">
        <div class="cf-grid">
          <span class="lb"><label for="c-neg">Default negative</label><InfoTip text="The workflow's own negative prompt: what to keep out of the image. Tags listed in Settings under “Removed from workflow negatives” are dropped when it's used." /></span>
          <textarea id="c-neg" class="textarea mono" rows="2" bind:value={draft.default_negative} use:grow></textarea>
          <span class="lb">Give the prompt writer the full notes<InfoTip text="Sends the workflow author's complete notes to the LLM with every plan. More context, but slower planning. Leave it off unless the notes matter." /></span>
          <button class="switch" role="switch" aria-checked={draft.use_full_notes} aria-label="Use full notes" onclick={() => (draft.use_full_notes = !draft.use_full_notes)}></button>
        </div>
      </section>

      <section class="cf-sec">
        <h4 class="hx">Read from the workflow<InfoTip text="Facts the app read from the workflow file. Slots are the inputs it can change per job, like prompt, seed and size." /></h4>
        <dl class="dl facts">
          <dt>Models</dt><dd class="mono">{draft.models.join(' · ') || 'None found'}</dd>
          {#if draft.default_loras.length}<dt>Own LoRAs</dt><dd class="mono">{draft.default_loras.map((l) => `${l.name}${l.weight != null ? ' @ ' + l.weight : ''}`).join(' · ')}</dd>{/if}
          {#if draft.input_images.length}<dt>Input images</dt><dd>{draft.input_images.join(' · ')}</dd>{/if}
        </dl>
        <details class="fold">
          <summary>Slots<span class="muted"> · read only</span></summary>
          <div class="slots-wrap" use:wheelScroll>
            <table class="slots">
              <thead><tr><th>Slot</th><th>Node · input</th><th>Default</th></tr></thead>
              <tbody>
                {#each slotRows(data.workflow) as [label, where, def]}
                  <tr><td>{label}</td><td class="mono">{where}</td><td class="mono def">{def}</td></tr>
                {/each}
              </tbody>
            </table>
          </div>
        </details>
        {#if draft.notes_full}
          <details class="fold">
            <summary>Notes from the workflow<span class="muted"> · {draft.notes_full.length.toLocaleString()} characters</span></summary>
            <pre class="notes">{draft.notes_full}</pre>
          </details>
        {/if}
      </section>
    </div>

    {#if dirty}
      <div class="savebar">
        <span class="muted">You have unsaved edits</span>
        <span class="spacer"></span>
        <button class="btn" onclick={discard} disabled={busy}>Discard</button>
        <button class="btn primary" onclick={save} disabled={busy}>Save edits</button>
      </div>
    {/if}
  {/if}
{/if}
{#snippet failed(error, reset)}
  <CardLoadFailed message={error?.message?.startsWith('https://svelte.dev') ? '' : error?.message} onretry={() => { reset(); retry() }} />
{/snippet}
</svelte:boundary>

<style>
  .bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .bar h4 { margin: 0; font-size: 13.5px; font-weight: 600; }
  .status { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12.5px; }
  .status.err { color: var(--red); }
  .attn { display: flex; gap: 8px; align-items: center; color: var(--amber); font-size: 13px; }
  .pending { padding: 14px; border: 1px solid var(--mint-line); border-radius: var(--r-xl); background: var(--mint-dim); }
  .ex { display: grid; grid-template-columns: minmax(0, 1fr) 28px; gap: 6px; align-items: start; }
  .range { display: flex; gap: 10px; flex-wrap: wrap; }
  .range label { display: grid; gap: 4px; font-size: 11.5px; color: var(--muted); font-weight: 500; }
  .range .field { width: 120px; }
  .facts { padding: 0; grid-template-columns: 110px minmax(0, 1fr); gap: 8px 16px; }
  .facts dd { overflow-wrap: anywhere; color: var(--text-soft); }
  .notes { margin: 0; padding: 12px; max-height: 320px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 12.5px/1.55 var(--mono); color: var(--text-soft); background: var(--bg); border-radius: var(--r-md); }
  .slots-wrap { overflow-x: auto; margin-top: 8px; }
  .slots { width: 100%; border-collapse: collapse; font-size: 13px; }
  .slots th { text-align: left; font-weight: 500; color: var(--muted); font-size: 12px; padding: 6px 12px 6px 0; border-bottom: 1px solid var(--line); }
  .slots td { padding: 7px 12px 7px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
  .slots tr:last-child td { border-bottom: 0; }
  .slots td:first-child { white-space: nowrap; color: var(--text-soft); }
  .slots td.def { color: var(--muted); overflow-wrap: anywhere; }
</style>
