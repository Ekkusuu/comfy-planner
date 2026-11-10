<script>
  import { onMount, tick } from 'svelte'
  import Icon from '../Icon.svelte'
  import InfoTip from '../InfoTip.svelte'
  import ComfyStatus from '../ComfyStatus.svelte'
  import CardEditor from '../CardEditor.svelte'
  import LoraCatalogue from '../LoraCatalogue.svelte'
  import DeleteDialog from '../DeleteDialog.svelte'
  import Wildcards from '../Wildcards.svelte'
  import { api } from '../api.js'
  import { isBuilding } from '../cards.js'
  import { fade } from '../motion.js'
  import { live, toast, toastError, refreshWorkflows, refreshWorkflowCards, refreshLoras, recheckWorkflows, comfyUp } from '../store.svelte.js'

  let open = $state(null) // workflow id
  let shown = $state(null) // last opened id, kept while the side sheet fades out
  let closeBtn = $state()
  let queuing = $state(false)
  let importing = $state(false)
  let saved = $state(null) // [{name, path}] or null while loading
  let busyPath = $state(null)
  let checking = $state(false)
  let broken = $state({}) // image id -> its cover failed to load
  let deleting = $state(null) // the workflow whose delete dialog is open: { id, title }

  const ready = $derived(live.workflows.filter((w) => w.runnable).length)
  const down = $derived(live.health !== null && !comfyUp())
  const wf = $derived(live.workflows.find((w) => w.id === (open ?? shown)))

  const cardOf = (id) => live.workflowCards.find((c) => c.key === id)
  const cardChip = (id) => {
    if (isBuilding(live.cardBuilds[`workflow:${id}`])) return { text: 'Writing', busy: true }
    const status = cardOf(id)?.status
    return status === 'approved' ? { text: 'Approved', cls: 'ok' } : status === 'draft' ? { text: 'Draft', cls: 'draft' } : { text: 'No card' }
  }
  const wfMissing = $derived(live.workflows.filter((w) => w.runnable && !cardOf(w.id)?.status))

  async function openSheet(id) {
    open = id
    shown = id
    await tick()
    closeBtn?.focus()
  }
  function closeSheet() {
    const id = open
    open = null
    if (id) tick().then(() => document.querySelector(`[data-wf="${CSS.escape(id)}"]`)?.focus())
  }
  /** Esc closes the side sheet or the import list and hands focus back to what opened it; typing in a field is left alone. */
  function onkey(e) {
    if (e.key !== 'Escape' || e.defaultPrevented || e.target.closest?.('input, textarea, select')) return
    if (open !== null) closeSheet()
    else if (importing) importing = false
  }
  // Models may have been added since the last visit: check again on arrival (quietly) and on the button.
  onMount(() => {
    recheckWorkflows()
    refreshWorkflowCards()
    refreshLoras()
  })
  /** Queue a build for each workflow that has no card; they run one after another with the LLM loaded once. */
  async function buildAll(keys) {
    queuing = true
    try {
      for (const key of keys) live.cardBuilds[`workflow:${key}`] = await api.buildCard('workflows', key)
      toast(`Writing ${keys.length} ${keys.length === 1 ? 'card' : 'cards'}. The queue waits for the current image.`)
    } catch (e) {
      toastError(e)
    } finally {
      queuing = false
    }
  }
  async function recheck() {
    checking = true
    const err = await recheckWorkflows()
    checking = false
    if (err) toastError(err)
    else toast('Checked against ComfyUI')
  }
  async function showImport() {
    importing = !importing
    if (importing) {
      saved = null
      try { saved = await api.savedWorkflows() } catch (e) { saved = []; toastError(e) }
    }
  }
  const importedFor = (path) => live.workflows.find((w) => w.source_path === path)
  async function doImport(item) {
    busyPath = item.path
    try {
      const wf = await api.importWorkflow(item.path)
      await refreshWorkflows()
      refreshWorkflowCards()
      toast(wf.runnable ? `${wf.title} is ready` : `${wf.title} imported, but it can’t run yet`)
    } catch (e) {
      toastError(e)
    } finally {
      busyPath = null
    }
  }

  /** The workflow is gone from the list: close its sheet too. */
  function deleted() {
    const id = deleting.id
    deleting = null
    if (open === id) open = null
  }
  const missingText = (m) => (m.input ? `${m.value}` : `${m.class}`)
</script>

<svelte:window onkeydown={onkey} />

<main class="page">
  <div class="head"><h1>Library</h1><span class="spacer"></span><ComfyStatus /></div>

  <section class="stack lg">
    <div class="section-head">
      <h2>Workflows</h2>
      <InfoTip title="Workflows" text="Image recipes you built in ComfyUI. Import one and the app keeps its own copy, then the LLM writes a card describing it so the planner knows when to use it. Approve the card after you've checked it." />
      {#if live.workflows.length}<span class="n">{ready} of {live.workflows.length} ready to run</span><InfoTip text="Ready means every model file the workflow needs is on disk. Missing files means ComfyUI can't find one; add it, then press Re-check." label="What ready means" />{/if}
      <span class="spacer"></span>
      {#if wfMissing.length}
        <button class="btn" onclick={() => buildAll(wfMissing.map((w) => w.id))} disabled={queuing} title="Write draft cards for the workflows that have none"><Icon name="spark" />Build {wfMissing.length} missing {wfMissing.length === 1 ? 'card' : 'cards'}</button>
      {/if}
      <button class="btn" onclick={recheck} disabled={checking || down} title="Check the models again, after adding files or restarting ComfyUI">
        <span class:spin={checking} style="display:grid"><Icon name="retry" /></span>Re-check
      </button>
      <button class="btn" onclick={showImport} aria-expanded={importing}><Icon name={importing ? 'x' : 'import'} />{importing ? 'Close' : 'Import'}</button>
    </div>

    <div class="reveal" class:open={importing}>
      <div>
        <div class="composer" style="gap:6px">
          <h3>Saved in ComfyUI</h3>
          {#if down}<p class="muted" style="font-size:13px">ComfyUI is offline. Importing needs it running, so it can check your model files.</p>{/if}
          {#if saved === null}
            <p class="muted">Looking for workflows…</p>
          {:else if !saved.length}
            <p class="muted">No saved workflows found in ComfyUI’s user folder.</p>
          {:else}
            <ul class="imports">
              {#each saved as item (item.path)}
                {@const have = importedFor(item.path)}
                <li>
                  <span class="nm">{item.name}</span>
                  {#if have}<span class="muted" style="font-size:12.5px">{have.runnable ? 'Imported' : 'Imported, not runnable'}</span>{/if}
                  <button class="btn sm" disabled={busyPath !== null || down} onclick={() => doImport(item)}>
                    {#if busyPath === item.path}<span class="spin" style="display:grid"><Icon name="loader" /></span>Importing{:else}{have ? 'Update' : 'Import'}{/if}
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
      </div>
    </div>

    {#if !live.ready}
      <div class="skeleton" aria-hidden="true"></div>
    {:else if !live.workflows.length}
      <div class="empty fade-in">
        <div class="glyph"><Icon name="box" /></div>
        <h3>No workflows yet</h3>
        <p>Import a workflow you saved in ComfyUI. The app keeps its own converted copy and never changes your original.</p>
        <button class="btn primary" onclick={showImport}><Icon name="import" />Import</button>
      </div>
    {:else}
      <div class="wfgrid">
        {#each live.workflows as w (w.id)}
          {@const chip = cardChip(w.id)}
          <div class="wf-cell">
            <button class="wf" class:open={open === w.id} data-wf={w.id} onclick={() => openSheet(w.id)} aria-haspopup="dialog">
              <span class="cover">
                <span class="chip-card {chip.cls ?? ''}">{#if chip.busy}<span class="spin" style="display:grid"><Icon name="loader" /></span>{/if}{chip.text}</span>
                {#if w.cover_image_id !== null && !broken[w.cover_image_id]}
                  <img src={api.thumbUrl(w.cover_image_id)} alt="" loading="lazy" decoding="async" onerror={() => (broken[w.cover_image_id] = true)} />
                {:else}
                  <span class="ph"><Icon name="image" /><span>No images yet</span></span>
                {/if}
              </span>
              <span class="meta">
                <span class="wf-title" title={w.title}>{w.title}</span>
                <span class="sub">
                  <span class="kind">{w.kind === 'edit' ? 'Image edit' : 'Text to image'}</span>
                  <span class="spacer"></span>
                  {#if w.runnable}
                    <span class="state"><span class="dot"></span>Ready</span>
                  {:else}
                    <span class="state warn"><span class="dot off"></span>Missing {w.missing.length} {w.missing.length === 1 ? 'file' : 'files'}</span>
                  {/if}
                </span>
              </span>
            </button>
            <button class="wf-del" aria-label="Delete {w.title}" title="Delete this workflow" onclick={() => (deleting = { id: w.id, title: w.title })}><Icon name="trash" /></button>
          </div>
        {/each}
      </div>
    {/if}
  </section>

  <LoraCatalogue />

  <Wildcards />
</main>

{#if open !== null && wf}
  <div class="sheetbox" transition:fade={{ duration: 180 }}>
    <button class="sheet-scrim" tabindex="-1" aria-label="Close" onclick={closeSheet}></button>
    <div class="sheet" role="dialog" aria-modal="true" aria-label="Workflow {wf.title}">
      <div class="sheet-head">
        <h3 title={wf.title}>{wf.title}</h3>
        <span class="spacer"></span>
        <button class="icon-btn danger" onclick={() => (deleting = { id: wf.id, title: wf.title })} aria-label="Delete {wf.title}" title="Delete this workflow"><Icon name="trash" /></button>
        <button class="icon-btn" bind:this={closeBtn} onclick={closeSheet} aria-label="Close details"><Icon name="x" /></button>
      </div>
      {#if !wf.runnable}
        <div class="miss">
          <p>{wf.missing.length ? 'These aren’t on disk yet, so this workflow can’t run:' : 'This workflow can’t run:'}</p>
          <ul>
            {#each wf.missing as m}<li><span class="mono">{missingText(m)}</span> <span class="muted">{m.class}{m.input ? `.${m.input}` : ''}</span></li>{/each}
            {#each wf.warnings.filter((x) => typeof x === 'string') as x}<li>{x}</li>{/each}
          </ul>
        </div>
      {/if}
      <CardEditor {wf} />
    </div>
  </div>
{/if}

{#if deleting}
  <DeleteDialog kind="workflow" target={deleting.id} name={deleting.title} ondone={deleted} onclose={() => (deleting = null)} />
{/if}

<style>
  .imports { list-style: none; margin: 4px 0 0; padding: 0; display: grid; }
  .imports li { display: flex; align-items: center; gap: 12px; padding: 7px 0; border-top: 1px solid var(--line); }
  .imports li:first-child { border-top: 0; }
  .imports .nm { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .wfgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 14px; }
  .wf-cell { position: relative; min-width: 0; display: grid; }
  .wf-del { position: absolute; top: 8px; right: 8px; z-index: 3; width: 26px; height: 26px; border-radius: 13px; display: grid; place-items: center; background: rgba(10, 10, 11, 0.78); backdrop-filter: blur(6px); border: 1px solid var(--line2); color: var(--text-soft); opacity: 0; transition: opacity 0.2s var(--ease), color 0.2s var(--ease), border-color 0.2s var(--ease), background 0.2s var(--ease); }
  .wf-del :global(svg.i) { width: 13px; height: 13px; }
  .wf-cell:hover .wf-del, .wf-del:focus-visible { opacity: 1; }
  .wf-del:hover, .wf-del:focus-visible { border-color: var(--red-line); color: var(--red); background: var(--red-dim); }
  @media (hover: none) { .wf-del { opacity: 1; } }
  .wf { display: grid; grid-template-rows: auto auto; align-content: start; text-align: left; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-2xl); overflow: hidden; transition: border-color 0.25s var(--ease), transform 0.15s var(--ease); }
  .wf:hover { border-color: var(--line2); }
  .wf.open { border-color: var(--mint-line); }
  .wf:focus-visible { outline: 2px solid var(--mint); outline-offset: 2px; }
  .wf:active { transform: scale(0.99); }
  .cover { position: relative; display: block; aspect-ratio: 4 / 3; background: var(--raised); overflow: hidden; }
  .cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transition: transform 0.6s var(--ease); animation: fadein 0.45s var(--ease) backwards; }
  .wf:hover .cover img { transform: scale(1.03); }
  .cover::after { content: ''; position: absolute; inset: 0; box-shadow: inset 0 -1px 0 var(--line); pointer-events: none; }
  .ph { position: absolute; inset: 0; display: grid; place-content: center; justify-items: center; gap: 8px; color: var(--faint); font-size: 12.5px; background: radial-gradient(120% 90% at 50% 0%, rgba(255, 255, 255, 0.04), transparent 70%); }
  .ph :global(svg.i) { width: 22px; height: 22px; }
  .meta { display: grid; gap: 8px; padding: 12px 14px 13px; min-width: 0; }
  .wf-title { font-weight: 500; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sub { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .kind { height: 22px; padding: 0 8px; border-radius: var(--r-sm); background: var(--raised); color: var(--muted); font-size: 11.5px; font-weight: 500; display: inline-flex; align-items: center; white-space: nowrap; }
  .state { display: inline-flex; align-items: center; gap: 7px; color: var(--muted); font-size: 12.5px; white-space: nowrap; }
  .state.warn { color: var(--amber); }

  .miss p { color: var(--amber); font-size: 13.5px; margin-bottom: 6px; }
  .miss ul { margin: 0; padding: 0 0 0 18px; display: grid; gap: 3px; font-size: 13px; }
  .chip-card { position: absolute; top: 10px; left: 10px; z-index: 1; height: 22px; padding: 0 8px; border-radius: var(--r-sm); background: rgba(10, 10, 11, 0.78); backdrop-filter: blur(6px); border: 1px solid var(--line2); color: var(--muted); font-size: 11.5px; font-weight: 500; display: inline-flex; align-items: center; gap: 6px; }
  .chip-card.draft { color: var(--amber); }
  .chip-card.ok { color: var(--mint); }
</style>
