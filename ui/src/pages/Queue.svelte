<script>
  import Icon from '../Icon.svelte'
  import InfoTip from '../InfoTip.svelte'
  import ComfyStatus from '../ComfyStatus.svelte'
  import QueueRow from '../QueueRow.svelte'
  import BatchRow from '../BatchRow.svelte'
  import { api } from '../api.js'
  import { live, go, toastError, upsertJob, refreshJobs, comfyUp, startComfy, ui, queueEntries, entryState } from '../store.svelte.js'

  // Rows: a single job, or one row for the jobs of a batch (a request that gave several jobs). A batch sits in the section its
  // state belongs to: running if any job runs, queued if any waits, otherwise done (all done) or failed/cancelled.
  // finished jobs you cleared with X stay out of the list (their images stay in the Gallery)
  const entries = $derived(queueEntries())
  const jobsOf = (e) => (e.batch ? e.jobs : [e.job])
  const newest = (e) => Math.max(...jobsOf(e).map((j) => j.id))
  const inState = (s) => entries.filter((e) => entryState(e) === s)
  const running = $derived(inState('running'))
  const queued = $derived(inState('queued'))
  const done = $derived(inState('done').sort((a, b) => newest(b) - newest(a)))
  const problems = $derived(inState('problems').sort((a, b) => newest(b) - newest(a)))
  const empty = $derived(!entries.length)
  const down = $derived(live.health !== null && !comfyUp())
  const paused = $derived(live.health?.queue?.paused)
  const held = $derived(live.health?.queue?.held)

  async function cancel(e) {
    try {
      if (e.batch) { await api.cancelBatch(e.id); await refreshJobs() } else upsertJob(await api.cancelJob(e.job.id))
    } catch (err) { toastError(err) }
  }
  async function retry(e) {
    try {
      if (e.batch) {
        for (const j of e.jobs.filter((x) => x.status === 'failed' || x.status === 'cancelled')) await api.retryJob(j.id, 'failed')
        await refreshJobs()
      } else upsertJob(await api.retryJob(e.job.id, e.job.status === 'done' ? 'all' : 'failed'))
    } catch (err) { toastError(err); refreshJobs() }
  }
  async function clear(e) {
    try {
      if (e.batch) { await api.clearBatch(e.id); await refreshJobs() } else upsertJob(await api.clearJob(e.job.id))
    } catch (err) { toastError(err) }
  }

  // ----- reorder queued rows: arrows or drag. A batch moves with all of its queued jobs. -----
  const queuedIds = (e) => jobsOf(e).filter((j) => j.status === 'queued').map((j) => j.id)
  async function reorder(list) {
    const ids = list.flatMap(queuedIds)
    // optimistic: give the rows their new positions now
    ids.forEach((id, n) => { const j = live.jobs.find((x) => x.id === id); if (j) j.position = n + 1 })
    live.jobs.sort((a, b) => a.position - b.position || a.id - b.id)
    try {
      await api.setOrder(ids)
    } catch (err) { toastError(err) }
    refreshJobs()
  }
  function move(e, delta) {
    const list = [...queued]
    const i = list.findIndex((x) => x.key === e.key)
    list.splice(Math.min(Math.max(i + delta, 0), list.length - 1), 0, list.splice(i, 1)[0])
    reorder(list)
  }
  let dragKey = $state(null)
  let overKey = $state(null)
  function drop(targetKey) {
    const from = dragKey
    dragKey = overKey = null
    if (from == null || from === targetKey) return
    const list = [...queued]
    const moved = list.splice(list.findIndex((x) => x.key === from), 1)[0]
    list.splice(list.findIndex((x) => x.key === targetKey), 0, moved)
    reorder(list)
  }
  const dragProps = (entry) => ({
    draggable: 'true',
    class: dragKey === entry.key ? 'dragging' : overKey === entry.key && dragKey !== null ? 'dragover' : '',
    ondragstart: (e) => { dragKey = entry.key; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', entry.key) },
    ondragover: (e) => { if (dragKey != null) { e.preventDefault(); overKey = entry.key } },
    ondragleave: () => (overKey = overKey === entry.key ? null : overKey),
    ondrop: (e) => { e.preventDefault(); drop(entry.key) },
    ondragend: () => (dragKey = overKey = null),
  })
  const openInGallery = (job) => go('gallery', { job: job.id })
</script>

<main class="page">
  <div class="head"><h1>Queue</h1><span class="spacer"></span><ComfyStatus /></div>

  {#if down}
    <div class="banner" role="alert">
      <Icon name="alert" />
      <div class="tx"><b>ComfyUI isn’t reachable at {live.health.comfy.url}.</b><span>{queued.length || running.length ? 'Your queue is safe and continues as soon as it’s back.' : 'Start it to run jobs.'}</span></div>
      <button class="btn" onclick={startComfy} disabled={ui.starting}>{ui.starting ? 'Starting' : 'Start ComfyUI'}</button>
    </div>
  {/if}
  {#if held}
    <div class="banner"><Icon name="alert" /><div class="tx"><b>Paused for planning.</b><span>The local LLM is using the GPU. The queue continues when it has been unloaded.</span></div><InfoTip text="Your graphics card can run the planning LLM or ComfyUI, not both. The queue waits while the LLM plans, then carries on by itself." label="Why is the queue held?" /></div>
  {/if}
  {#if paused}
    <div class="banner"><Icon name="alert" /><div class="tx"><b>The queue is paused.</b></div><InfoTip text="Nothing new starts while the queue is paused. Your jobs stay in order and wait." label="About the paused queue" /></div>
  {/if}

  {#if !live.ready}
    <div class="skeleton" aria-hidden="true"></div>
  {:else if empty}
    <div class="empty fade-in">
      <div class="glyph"><Icon name="queue" /></div>
      <h3>Nothing in the queue</h3>
      <p>Jobs you run from Plan wait here, and finished ones stay for reference.</p>
      <a class="btn primary" href="#/plan"><Icon name="plan" />Go to Plan</a>
    </div>
  {:else}
    {#if running.length}
      <section class="q-full">
        <div class="section-head"><h2>Running</h2><InfoTip text="ComfyUI makes one job at a time. The bar shows progress through the current image. A batch is one row: open it to see its jobs, cancel stops the rest of it." label="About running jobs" /></div>
        {#each running as entry (entry.key)}
          <div class="q-stage">
            {#if entry.batch}
              <BatchRow batch={entry} full stage oncancel={cancel} onopen={openInGallery} />
            {:else}
              <QueueRow job={entry.job} full stage oncancel={() => cancel(entry)} />
            {/if}
          </div>
        {/each}
      </section>
    {/if}

    {#if queued.length}
      <section class="q-full">
        <div class="section-head"><h2>Queued</h2><span class="n">{queued.length}</span><InfoTip text="Jobs run from the top down. Drag a row, or use the arrows, to change the order. Cancel takes a job out. A batch (the jobs of one request) is one row and moves as a whole." label="About the queue order" /></div>
        {#each queued as entry, i (entry.key)}
          {#if entry.batch}
            <BatchRow batch={entry} full grip first={i === 0} last={i === queued.length - 1} oncancel={cancel} onmove={(d) => move(entry, d)} onopen={openInGallery} {...dragProps(entry)} />
          {:else}
            <QueueRow job={entry.job} full grip first={i === 0} last={i === queued.length - 1} oncancel={() => cancel(entry)} onmove={(d) => move(entry, d)} {...dragProps(entry)} />
          {/if}
        {/each}
      </section>
    {/if}

    {#if done.length}
      <section class="q-full">
        <div class="section-head"><h2>Done</h2><span class="n">{done.length}</span><InfoTip text="X clears a job or a whole batch from this list. Its images stay in the Gallery." label="About clearing done jobs" /></div>
        {#each done as entry (entry.key)}
          {#if entry.batch}
            <BatchRow batch={entry} full onclear={clear} onopen={openInGallery} />
          {:else}
            <QueueRow job={entry.job} full onopen={openInGallery} onclear={() => clear(entry)} />
          {/if}
        {/each}
      </section>
    {/if}

    {#if problems.length}
      <section class="q-full">
        <div class="section-head"><h2>Failed and cancelled</h2><span class="n">{problems.length}</span><InfoTip text="Retry puts a job back in the queue and makes only the images that failed. X clears a job or batch from this list; any images it made stay in the Gallery." label="About retry and clear" /></div>
        {#each problems as entry (entry.key)}
          {#if entry.batch}
            <BatchRow batch={entry} full onretry={retry} onclear={clear} onopen={openInGallery} />
          {:else}
            <QueueRow job={entry.job} full onretry={() => retry(entry)} onopen={openInGallery} onclear={() => clear(entry)} />
          {/if}
        {/each}
      </section>
    {/if}
  {/if}
</main>
