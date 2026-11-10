<script>
  // One job in the queue: thumbnail, title, state line, progress, actions. Used on Plan (side) and Queue (full).
  import Icon from './Icon.svelte'
  import LivePreview from './LivePreview.svelte'
  import { api } from './api.js'
  import { live, thumbFor, jobTitle, workflowTitle } from './store.svelte.js'

  let { job, full = false, stage = false, oncancel, onretry, onclear, onmove, onopen, first = false, last = false, grip = false, class: cls = '', ...rest } = $props()

  const counts = $derived(job.image_counts ?? {})
  const done = $derived(counts.done ?? 0)
  const p = $derived(live.progress[job.id])
  const thumb = $derived(thumbFor(job.id))

  // Live look at the image being generated: the newest step preview, swapped in only once it has loaded (no flicker).
  let frame = $state(null) // { job, url } of the newest preview that has loaded
  $effect(() => {
    const number = live.preview[job.id]
    if (!number) return
    const id = job.id
    const url = api.previewUrl(id, number)
    const img = new Image()
    let stale = false
    img.onload = () => { if (!stale) frame = { job: id, url } }
    img.src = url
    return () => { stale = true }
  })
  // Only while this job runs; after its last image, until the finished thumbnail is there. Never after a fail or cancel.
  const showFrame = $derived(!!frame && frame.job === job.id && (job.status === 'running' ? !!live.preview[job.id] : job.status === 'done' && !thumb))
  const pct = $derived(Math.min(100, Math.round(((done + (p?.frac ?? 0)) / Math.max(job.count, 1)) * 100)))
  const secs = $derived(job.started_at && job.finished_at ? Math.max(1, Math.round((new Date(job.finished_at) - new Date(job.started_at)) / 1000)) : null)
  const dur = (s) => (s < 90 ? `${s} s` : `${Math.round(s / 60)} min`)
  const plural = (n) => `${n} ${n === 1 ? 'image' : 'images'}`

  const line = $derived.by(() => {
    switch (job.status) {
      case 'running': return p ? `Image ${Math.min(p.idx, job.count)} of ${job.count}` : 'Starting'
      case 'queued': return `Waiting · ${plural(job.count)}`
      case 'done': return `${plural(job.count)}${secs ? ` · ${dur(secs)}` : ''}`
      case 'failed': return job.error || 'Failed'
      case 'cancelled': return done ? `Cancelled · ${done} of ${job.count} done` : 'Cancelled'
      default: return job.status
    }
  })
</script>

<div class="q {cls}" class:sel={!!onopen && !!thumb} class:running={job.status === 'running'} class:stage {...rest}>
  {#if stage}
    <LivePreview jobId={job.id} scope={`job-${job.id}`} />
  {:else if showFrame}
    <img class="thumb" src={frame.url} alt="" decoding="async" />
  {:else if thumb}
    <img class="thumb" src={api.thumbUrl(thumb.id, 128)} alt="" loading="lazy" decoding="async" />
  {:else}
    <span class="thumb ph"><Icon name="image" /></span>
  {/if}
  <div style="min-width:0">
    {#if onopen && thumb}
      <button class="t cover" title={jobTitle(job)} onclick={() => onopen(job)}>{jobTitle(job)}</button>
    {:else}
      <div class="t" title={jobTitle(job)}>{jobTitle(job)}</div>
    {/if}
    <div class="s" class:err={job.status === 'failed'} title={full ? workflowTitle(job.workflow_id) : line}>{line}{#if full && job.status !== 'failed'}{' · ' + workflowTitle(job.workflow_id)}{/if}</div>
    {#if job.status === 'running'}
      <div class="prog" class:indet={!p} role="progressbar" aria-label="Job progress" aria-valuenow={pct} aria-valuemin="0" aria-valuemax="100"><i style="width:{pct}%"></i></div>
    {/if}
  </div>
  <div class="acts">
    {#if full && job.status === 'queued'}
      {#if grip}<span class="icon-btn sm grip" aria-hidden="true" title="Drag to reorder"><Icon name="grip" /></span>{/if}
      <button class="icon-btn sm" aria-label="Move up" disabled={first} onclick={(e) => { e.stopPropagation(); onmove(-1) }}><Icon name="up" /></button>
      <button class="icon-btn sm" aria-label="Move down" disabled={last} onclick={(e) => { e.stopPropagation(); onmove(1) }}><span style="display:grid;transform:rotate(180deg)"><Icon name="up" /></span></button>
    {/if}
    {#if (job.status === 'failed' || job.status === 'cancelled') && onretry}
      <button class="icon-btn" aria-label="Retry job" title="Retry" onclick={(e) => { e.stopPropagation(); onretry(job) }}><Icon name="retry" /></button>
    {/if}
    {#if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') && onclear}
      <button class="icon-btn" aria-label="Clear from the queue" title="Clear from the queue" onclick={(e) => { e.stopPropagation(); onclear(job) }}><Icon name="x" /></button>
    {/if}
    {#if (job.status === 'queued' || job.status === 'running') && oncancel}
      <button class="icon-btn" aria-label="Cancel job" title="Cancel" onclick={(e) => { e.stopPropagation(); oncancel(job) }}><Icon name="x" /></button>
    {/if}
  </div>
</div>
