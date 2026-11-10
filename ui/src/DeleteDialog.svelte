<script>
  // The confirm dialog for deleting a LoRA, a workflow or a wildcard list together with its files. It first asks the app what
  // a delete would remove (nothing is deleted yet), shows exactly that, and deletes on one click. Files go to the Recycle Bin.
  // A workflow also offers the model files nothing else uses, unticked; the ones another workflow uses are listed as kept.
  import { onMount } from 'svelte'
  import Icon from './Icon.svelte'
  import { api } from './api.js'
  import { fade, pop } from './motion.js'
  import { afterDelete, toast, toastError } from './store.svelte.js'

  let { kind, target, name, ondone, onclose } = $props() // kind: lora | workflow | wildcard; target: its key; name: how to call it

  const WHAT = { lora: 'LoRA', workflow: 'workflow', wildcard: 'wildcard list' }

  let plan = $state(null)
  let failed = $state('') // why the plan could not be had
  let busy = $state(false)
  let ticked = $state({}) // model id -> also delete this model file
  let cancelJobs = $state(false) // also cancel the queued jobs that use it
  let box = $state()
  let cancelBtn = $state()

  const offered = $derived((plan?.models ?? []).filter((m) => m.removable))
  const kept = $derived((plan?.models ?? []).filter((m) => !m.removable))
  const chosen = $derived(offered.filter((m) => ticked[m.id]))
  const sending = $derived([...(plan?.files ?? []), ...chosen])
  const total = $derived(sending.reduce((n, f) => n + f.size, 0))
  const jobs = $derived(plan?.jobs ?? { running: [], queued: [], open: [] })

  function size(bytes) {
    if (bytes < 1024) return `${bytes} B`
    const units = ['KB', 'MB', 'GB']
    let n = bytes / 1024
    let i = 0
    while (n >= 1024 && i < units.length - 1) (n /= 1024), i++
    return `${n >= 100 ? Math.round(n) : n.toFixed(1)} ${units[i]}`
  }
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`

  onMount(async () => {
    cancelBtn?.focus()
    try {
      plan = await api.deletePlan(kind, target)
    } catch (e) {
      failed = e.message
    }
  })

  async function confirm() {
    busy = true
    try {
      await api.deleteItem(kind, target, { models: chosen.map((m) => m.id), cancel_jobs: cancelJobs })
      afterDelete(kind)
      toast(sending.length ? `Deleted ${name}. ${count(sending.length, 'file is', 'files are')} in the Recycle Bin.` : `Deleted ${name}`)
      ondone()
    } catch (e) {
      toastError(e)
      busy = false
      try { plan = await api.deletePlan(kind, target) } catch {} // what is left to delete may have changed
    }
  }
  /** Esc closes the dialog (and nothing behind it); Tab stays inside it. */
  function onkey(e) {
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      if (!busy) onclose()
    } else if (e.key === 'Tab') {
      const list = [...box.querySelectorAll('button:not(:disabled), input:not(:disabled)')]
      const at = list.indexOf(document.activeElement)
      if (!list.length) return
      if (e.shiftKey && at <= 0) (e.preventDefault(), list.at(-1).focus())
      else if (!e.shiftKey && at === list.length - 1) (e.preventDefault(), list[0].focus())
    }
  }
</script>

<div class="dd-wrap" transition:fade={{ duration: 160 }}>
  <button class="dd-scrim" tabindex="-1" aria-label="Close" onclick={() => !busy && onclose()}></button>
  <div class="dd" bind:this={box} role="alertdialog" aria-modal="true" aria-labelledby="dd-title" aria-describedby="dd-intro" onkeydown={onkey} transition:pop={{ duration: 220, y: 8 }}>
    <div class="dd-head">
      <span class="dd-icon"><Icon name="trash" /></span>
      <h3 id="dd-title">Delete {WHAT[kind]} <span class="mono dd-name">{name}</span>?</h3>
    </div>

    {#if failed}
      <p class="dd-err" role="alert"><Icon name="alert" />{failed}</p>
    {:else if !plan}
      <p class="muted">Looking at what would be deleted…</p>
    {:else}
      <p id="dd-intro" class="muted dd-intro">These files go to the Windows Recycle Bin, so you can still restore them from there. Nothing is deleted until you press Delete.</p>

      {#if plan.blocked}
        <p class="dd-block" role="alert"><Icon name="alert" />{plan.blocked}</p>
      {/if}

      <div class="dd-body">
        <section aria-label="Files that go to the Recycle Bin">
          <h4>Moved to the Recycle Bin</h4>
          {#if plan.files.length}
            <ul class="dd-files">
              {#each plan.files as f (f.full)}
                <li title={f.full}><span class="dd-path mono">{f.path}</span><span class="dd-in muted">{f.folder}</span><span class="dd-size">{size(f.size)}</span></li>
              {/each}
            </ul>
          {:else}
            <p class="muted">No files. Only the app’s records are removed.</p>
          {/if}
        </section>

        {#if plan.models.length}
          <section aria-label="Model files this workflow uses">
            <h4>Model files it uses</h4>
            {#if offered.length}
              <p class="muted dd-sub">Nothing else uses these. Tick the ones you want to delete too.</p>
              <ul class="dd-files">
                {#each offered as m (m.id)}
                  <li title={m.full}>
                    <label class="dd-pick">
                      <input type="checkbox" bind:checked={ticked[m.id]} disabled={busy} />
                      <span class="dd-path mono">{m.path}</span>
                      <span class="dd-kind muted">{m.kind} · {m.folder}</span>
                    </label>
                    <span class="dd-size">{size(m.size)}</span>
                  </li>
                {/each}
              </ul>
            {/if}
            {#if kept.length}
              <p class="muted dd-sub">{offered.length ? 'Kept:' : 'All of its model files stay:'}</p>
              <ul class="dd-files kept">
                {#each kept as m (m.id)}
                  <li title={m.full}><span class="dd-path mono">{m.path}</span><span class="dd-why muted">{m.why_kept}</span><span class="dd-size">{size(m.size)}</span></li>
                {/each}
              </ul>
            {/if}
          </section>
        {/if}

        {#if plan.records.length}
          <section aria-label="Removed from the app">
            <h4>Removed from the app</h4>
            <ul class="dd-list">{#each plan.records as r}<li>{r}</li>{/each}</ul>
          </section>
        {/if}

        {#if jobs.queued.length}
          <section class="dd-warn" aria-label="Queued jobs">
            <p><Icon name="alert" />{count(jobs.queued.length, 'queued job uses', 'queued jobs use')} it ({jobs.queued.map((j) => j.label).join(', ')}). {kind === 'lora' ? 'They will fail when they run.' : 'They can’t run without it.'}</p>
            <label class="dd-tick"><input type="checkbox" bind:checked={cancelJobs} disabled={busy} />Cancel {count(jobs.queued.length, 'this job', 'these jobs')} as well</label>
          </section>
        {/if}
        {#if jobs.open.length}
          <p class="muted dd-sub">{count(jobs.open.length, 'job not queued yet uses', 'jobs not queued yet use')} it{kind === 'workflow' ? ' and will become a draft without a workflow' : ''}.</p>
        {/if}

        {#each plan.kept as k}<p class="muted dd-sub">{k}</p>{/each}
      </div>
    {/if}

    <div class="dd-foot">
      <span class="muted dd-total">{plan && sending.length ? `${count(sending.length, 'file', 'files')} · ${size(total)}` : ''}</span>
      <span class="spacer"></span>
      <button class="btn" bind:this={cancelBtn} onclick={onclose} disabled={busy}>Cancel</button>
      <button class="btn danger armed" onclick={confirm} disabled={busy || !plan || !!plan.blocked}>
        {#if busy}<span class="spin" style="display:grid"><Icon name="loader" /></span>Deleting{:else}<Icon name="trash" />Delete{/if}
      </button>
    </div>
  </div>
</div>

<style>
  .dd-wrap { position: fixed; inset: 0; z-index: 80; display: grid; place-items: center; padding: 16px; }
  .dd-scrim { position: absolute; inset: 0; background: rgba(0, 0, 0, 0.6); cursor: default; }
  .dd { position: relative; width: min(560px, 100%); max-height: min(720px, 100%); display: grid; grid-template-rows: auto auto minmax(0, 1fr) auto; gap: 12px; padding: 18px 20px 16px; background: var(--float); border: 1px solid var(--line2); border-radius: var(--r-2xl); box-shadow: var(--shadow-overlay); }
  .dd-head { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .dd-icon { flex: none; width: 32px; height: 32px; border-radius: var(--r-lg); display: grid; place-items: center; background: var(--red-dim); color: var(--red); border: 1px solid var(--red-line); }
  .dd-head h3 { font-size: 15px; font-weight: 600; min-width: 0; overflow-wrap: anywhere; }
  .dd-name { font-weight: 500; color: var(--text-soft); }
  .dd-intro { font-size: 13px; line-height: 1.5; }
  .dd-body { display: grid; gap: 14px; align-content: start; overflow-y: auto; overscroll-behavior: contain; min-height: 0; padding-right: 4px; scrollbar-width: thin; }
  .dd-body section { display: grid; gap: 6px; }
  .dd h4 { font-size: 12.5px; font-weight: 600; color: var(--text-soft); }
  .dd-sub { font-size: 12.5px; line-height: 1.5; }
  .dd-files { list-style: none; margin: 0; padding: 0; display: grid; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface); }
  .dd-files li { display: flex; align-items: center; gap: 10px; padding: 7px 10px; min-width: 0; }
  .dd-files li + li { border-top: 1px solid var(--line); }
  .dd-path { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dd-in, .dd-kind, .dd-why { flex: none; font-size: 12px; white-space: nowrap; }
  .dd-why { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; text-align: right; }
  .dd-size { flex: none; margin-left: auto; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .dd-why + .dd-size { margin-left: 0; }
  .dd-files.kept .dd-path { color: var(--muted); }
  .dd-pick { flex: 1 1 auto; min-width: 0; display: flex; align-items: center; gap: 10px; cursor: pointer; }
  .dd-pick .dd-path { flex: 0 1 auto; }
  .dd-pick input, .dd-tick input { flex: none; width: 15px; height: 15px; margin: 0; accent-color: var(--red); cursor: pointer; }
  .dd-list { margin: 0; padding: 0 0 0 18px; display: grid; gap: 3px; font-size: 13px; color: var(--text-soft); }
  .dd-warn { padding: 10px 12px; border: 1px solid var(--amber-line); border-radius: var(--r-md); background: var(--amber-dim); }
  .dd-warn p { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; line-height: 1.5; color: var(--amber); }
  .dd-warn p :global(svg.i), .dd-block :global(svg.i), .dd-err :global(svg.i) { flex: none; margin-top: 2px; }
  .dd-tick { display: flex; align-items: center; gap: 9px; font-size: 13px; cursor: pointer; }
  .dd-block, .dd-err { display: flex; gap: 8px; align-items: flex-start; padding: 10px 12px; border: 1px solid var(--red-line); border-radius: var(--r-md); background: var(--red-dim); color: var(--red); font-size: 13px; line-height: 1.5; }
  .dd-foot { display: flex; align-items: center; gap: 10px; padding-top: 4px; }
  .dd-total { font-size: 12.5px; font-variant-numeric: tabular-nums; }
  .dd input:focus-visible, .dd button:focus-visible { outline: 2px solid var(--mint); outline-offset: 2px; }
</style>
