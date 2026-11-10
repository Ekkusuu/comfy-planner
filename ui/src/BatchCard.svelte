<script>
  // A batch on the Plan page: the jobs one request made, as one card. Compact rows; opening a row shows the normal job card,
  // so every job can still be edited, locked and re-planned on its own.
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import JobCard from './JobCard.svelte'
  import { live, workflowTitle, blockingIssues, clip } from './store.svelte.js'

  let { batch, wfs, oncancelplan, onrunall, onreplanmany, onremoveall, ...cardHandlers } = $props()

  const jobs = $derived(batch.jobs)
  const total = $derived(jobs.length)
  const planned = $derived(jobs.filter((j) => j.status !== 'planning').length)
  const failed = $derived(jobs.filter((j) => j.status === 'draft'))
  const images = $derived(jobs.reduce((n, j) => n + j.count, 0))
  const complete = (j) => (j.fields?.prompts?.value ?? []).every((p) => p.trim())
  const ready = $derived(jobs.filter((j) => j.status === 'ready' && !blockingIssues(j).length && complete(j)))
  const readyImages = $derived(ready.reduce((n, j) => n + j.count, 0))
  const working = $derived(planned < total)
  const pct = $derived(Math.round((planned / Math.max(total, 1)) * 100))

  let open = $state(false)
  let opened = $state({}) // job id -> its card is shown
  let confirming = $state(false)

  const stateOf = (j) => {
    if (j.status === 'planning') return ['Planning', 'wait']
    if (j.status === 'draft') return j.error ? ['Not planned', 'err'] : ['Needs attention', 'warn']
    if (blockingIssues(j).length || !complete(j)) return ['Needs attention', 'warn']
    return ['Ready', 'ok']
  }
  const loraText = (j) => (j.fields?.loras?.value ?? []).map((l) => l.name.replace(/\.(safetensors|ckpt|pt)$/i, '')).join(', ')
</script>

<section class="job arrive bx-card" class:busy={working}>
  <div class="jh">
    <button class="bx-toggle" aria-expanded={open} onclick={() => (open = !open)} title={batch.request}>
      <span class="bx-chev" class:bx-open={open}><Icon name="chevron" /></span>
      <h3>{clip(batch.request || 'Batch', 110)}</h3>
    </button>
    <InfoTip title="Batch" text="One request that gave several images. Each job in it was planned on its own, so its LoRAs, prompt and settings can differ. Open a row to edit that job like any other card. Run all queues every job that is ready." label="About batches" />
    <span class="spacer"></span>
    {#if working && oncancelplan}
      <button class="btn sm" onclick={oncancelplan} title="Stop planning. What is planned stays, the rest becomes drafts you can plan later."><Icon name="x" />Stop planning</button>
    {/if}
    {#if !working && failed.length}
      <button class="btn sm" onclick={() => onreplanmany(failed)} title="Plan the jobs that failed or were not reached again. Everything you locked stays as it is."><Icon name="retry" />Re-plan failed ({failed.length})</button>
    {/if}
    <button class="btn sm" onclick={() => onrunall(batch)} disabled={!ready.length || working}><Icon name="play" />Run all{ready.length ? ` (${readyImages})` : ''}</button>
    {#if confirming}
      <span class="bx-ask">Remove {total === 1 ? 'this card' : `these ${total} cards`}?</span>
      <button class="btn sm" onclick={() => (confirming = false)}>Cancel</button>
      <button class="btn sm danger armed" onclick={() => { confirming = false; onremoveall(batch) }}><Icon name="trash" />Remove</button>
    {:else}
      <button class="btn sm danger" onclick={() => (confirming = true)} disabled={working} title="Remove these cards. Nothing has been queued yet."><Icon name="trash" />Remove</button>
    {/if}
  </div>

  <p class="bx-count">
    <b>{planned} of {total}</b> planned · {images} {images === 1 ? 'image' : 'images'} · {workflowTitle(jobs[0]?.workflow_id)}
    {#if failed.length && !working}<span class="bx-flag">· {failed.length} need{failed.length === 1 ? 's' : ''} attention</span>{/if}
  </p>
  {#if working}<div class="prog" role="progressbar" aria-label="Planning progress" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100" style="margin:0 0 10px"><i style="width:{pct}%"></i></div>{/if}

  {#if open}
    <ul class="bx-rows">
      {#each jobs as job, i (job.id)}
        {@const [label, tone] = stateOf(job)}
        <li class="bx-item" class:bx-on={opened[job.id]}>
          <button class="bx-row" aria-expanded={!!opened[job.id]} onclick={() => (opened[job.id] = !opened[job.id])}>
            <span class="bx-no">{i + 1}</span>
            <span class="bx-text" title={job.description}>{job.description || 'Untitled job'}{#if job.count > 1}<span class="bx-n2"> · {job.count} images</span>{/if}</span>
            <span class="bx-loras" title={loraText(job)}>{loraText(job) || '—'}</span>
            <span class="bx-wf" title={workflowTitle(job.workflow_id)}>{workflowTitle(job.workflow_id)}</span>
            <span class="bx-state bx-{tone}">{label}</span>
          </button>
          {#if opened[job.id]}
            <div class="bx-open-card">
              {#if wfs[job.workflow_id] || !job.workflow_id}
                <JobCard {job} wf={wfs[job.workflow_id] ?? null} delay={0} {...cardHandlers} />
              {:else}
                <div class="skeleton"></div>
              {/if}
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .bx-toggle { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 0 1 auto; text-align: left; }
  .bx-toggle h3 { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; }
  .bx-chev { display: inline-grid; flex: none; color: var(--muted); transition: transform 0.3s var(--ease); transform: rotate(-90deg); }
  .bx-chev.bx-open { transform: none; }
  .bx-count { color: var(--muted); font-size: 12.5px; margin: 4px 0 10px; }
  .bx-count b { color: var(--text-soft); font-weight: 500; }
  .bx-flag { color: var(--amber); }
  .bx-ask { color: var(--text-soft); font-size: 13px; }
  .bx-rows { list-style: none; margin: 0 0 10px; padding: 0; border-top: 1px solid var(--line); max-height: 640px; overflow: auto; }
  .bx-item { border-bottom: 1px solid var(--line); }
  .bx-item:last-child { border-bottom: 0; }
  .bx-row { width: 100%; display: grid; grid-template-columns: 28px minmax(0, 1.8fr) minmax(0, 1fr) minmax(0, 1fr) 104px; gap: 12px; align-items: center; padding: 8px 4px; text-align: left; font-size: 13px; border-radius: var(--r-md); transition: background 0.2s var(--ease); }
  .bx-row:hover { background: var(--raised); }
  .bx-no { color: var(--faint); font-family: var(--mono); font-size: 12px; }
  .bx-text { color: var(--text-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bx-n2 { color: var(--muted); }
  .bx-loras, .bx-wf { color: var(--muted); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bx-state { font-size: 12px; text-align: right; white-space: nowrap; color: var(--muted); }
  .bx-ok { color: var(--mint); }
  .bx-warn { color: var(--amber); }
  .bx-err { color: var(--red); }
  .bx-on .bx-row { background: var(--raised); }
  .bx-open-card { padding: 10px 0 14px 28px; }
  @media (max-width: 900px) { .bx-row { grid-template-columns: 24px minmax(0, 1fr) 96px; } .bx-loras, .bx-wf { display: none; } }
</style>
