<script>
  import { onMount, tick } from 'svelte'
  import Icon from '../Icon.svelte'
  import InfoTip from '../InfoTip.svelte'
  import ComfyStatus from '../ComfyStatus.svelte'
  import JobCard from '../JobCard.svelte'
  import DropZone from '../DropZone.svelte'
  import WorkflowPicker from '../WorkflowPicker.svelte'
  import QueueRow from '../QueueRow.svelte'
  import BatchRow from '../BatchRow.svelte'
  import BatchCard from '../BatchCard.svelte'
  import RequestBox from '../RequestBox.svelte'
  import PlanStatus from '../PlanStatus.svelte'
  import QueuedPlan from '../QueuedPlan.svelte'
  import { api } from '../api.js'
  import { live, go, toast, toastError, upsertJob, removeJob, getWorkflow, refreshJobs, refreshPlans, prefill, runningJobs, queuedJobs, activePlans, isActivePlan, blockingIssues, groupJobs, queueEntries, entryState } from '../store.svelte.js'
  import { leave } from '../motion.js'

  const drafts = $derived(live.jobs.filter((j) => j.status === 'ready' || j.status === 'draft' || j.status === 'planning'))
  const runnable = $derived(live.workflows.filter((w) => w.runnable))
  const cards = $derived(groupJobs(drafts)) // a request that gave several jobs is one batch card
  const sideJobs = $derived(queueEntries().filter((e) => ['running', 'queued'].includes(entryState(e))).slice(0, 6))
  // first-run hint: shown until dismissed once
  let hint = $state(localStorage.getItem('cp.hint.how') !== '1')
  function dismissHint() {
    hint = false
    localStorage.setItem('cp.hint.how', '1')
  }
  const maxImages = $derived(live.config.max_images_per_job)

  let wfs = $state({}) // full workflows by id (slots + defaults)
  $effect(() => {
    for (const id of new Set(drafts.map((j) => j.workflow_id).filter(Boolean))) {
      if (!wfs[id]) getWorkflow(id).then((w) => (wfs[id] = w)).catch(toastError)
    }
  })

  // ----- add job panel -----
  let adding = $state(false)
  let pick = $state(null)
  let count = $state(1)
  let images = $state([])
  let busy = $state(false)
  const notReady = $derived(live.workflows.length - runnable.length)
  const pickedWf = $derived(runnable.find((w) => w.id === pick))
  const pickedFull = $derived(pick ? wfs[pick] : null)
  $effect(() => {
    if (pick && !wfs[pick]) getWorkflow(pick).then((w) => (wfs[pick] = w)).catch(toastError)
  })
  const neededImages = $derived(pickedFull?.slots?.input_images ?? [])
  const canCreate = $derived(!!pickedWf && !busy && (!pickedWf.kind || pickedWf.kind !== 'edit' || (neededImages.length > 0 && !!images[0])))

  function openPanel() {
    adding = true
    if (!pick || !runnable.some((w) => w.id === pick)) pick = runnable[0]?.id ?? null
  }
  /** Esc inside the panel closes it and returns to the Add job button. */
  async function panelKey(e) {
    if (e.key === 'Escape') {
      adding = false
      await tick() // the header's button is gone while there are no jobs; the empty state has one too
      ;(document.getElementById('add-job') ?? document.querySelector('.empty .btn'))?.focus()
    } else if (e.key === 'Enter' && e.target.matches('.stepper input')) create()
  }
  const auto = (value, reason = 'workflow default') => ({ value, source: 'auto', reason })

  /** Starting fields of a new job: the workflow's own defaults, all "auto". */
  function defaultFields(wf, n, input) {
    const d = wf.slots.defaults ?? {}
    const f = {}
    for (const k of ['steps', 'cfg', 'sampler', 'scheduler', 'width', 'height', 'negative']) if (d[k] != null) f[k] = auto(d[k])
    if (wf.slots.hires) {
      f.hires = auto(live.config.hires_default)
      if (d.hires?.steps != null) f.hires_steps = auto(d.hires.steps)
      if (d.hires?.denoise != null) f.hires_denoise = auto(d.hires.denoise)
    }
    f.seed_strategy = auto('random', 'new seed per image')
    f.seed = auto(d.seed ?? Math.floor(Math.random() * 1e12))
    f.loras = auto((d.loras ?? []).filter((l) => l.name).map((l) => ({ name: l.name, weight: l.weight ?? 1 })), 'the workflow’s own LoRA')
    // a new job starts from the card's quality tags, never from the sample prompt saved in the workflow
    const prefix = live.workflowCards.find((c) => c.key === wf.id)?.quality_prefix ?? ''
    f.prompts = auto(Array(n).fill(prefix ? `${prefix}, ` : ''))
    if (input?.length) f.input_images = { value: input, source: 'locked', reason: 'set by hand' }
    return f
  }

  async function create() {
    if (!canCreate) return
    busy = true
    try {
      const wf = await getWorkflow(pick)
      const job = await api.createJob({ workflow_id: pick, count, fields: defaultFields(wf, count, wf.kind === 'edit' ? images.slice(0, neededImages.length) : null) })
      upsertJob(job)
      adding = false
      images = []
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }

  async function duplicate(job) {
    try {
      const copy = await api.createJob({ workflow_id: job.workflow_id, count: job.count, fields: job.fields, description: job.description })
      upsertJob(copy)
    } catch (e) {
      toastError(e)
    }
  }
  async function remove(job) {
    try {
      await api.deleteJob(job.id)
      removeJob(job.id)
    } catch (e) {
      toastError(e)
      refreshJobs()
    }
  }

  // ----- "Use as new job" from the gallery -----
  async function fromRecipe(r) {
    try {
      const wf = await getWorkflow(r.workflow_id)
      const f = defaultFields(wf, 1, r.input_images?.length ? r.input_images : null)
      for (const k of ['steps', 'cfg', 'sampler', 'scheduler', 'width', 'height', 'negative', 'hires_steps', 'hires_denoise']) {
        if (r[k] != null && k in f) f[k] = auto(r[k], 'from the image’s recipe')
      }
      if (wf.slots.hires && r.hires != null) f.hires = auto(!!r.hires, 'from the image’s recipe')
      f.loras = auto(r.loras ?? [], 'from the image’s recipe')
      f.prompts = auto([r.prompt ?? ''], 'from the image’s recipe')
      const job = await api.createJob({ workflow_id: r.workflow_id, count: 1, fields: f })
      upsertJob(job)
      toast('Added to Plan')
    } catch (e) {
      toastError(e)
    }
  }
  onMount(() => {
    if (prefill.recipe) {
      const r = prefill.recipe
      prefill.recipe = null
      fromRecipe(r)
    }
  })

  // ----- run -----
  const complete = (j) => (j.fields.prompts?.value ?? []).every((p) => p.trim())
  const canRun = (j) => j.status === 'ready' && !blockingIssues(j).length && complete(j)
  const runnableDrafts = $derived(drafts.filter(canRun))
  const total = $derived(runnableDrafts.reduce((n, j) => n + j.count, 0))
  let running = $state(false)
  async function runAll() {
    if (!total || running) return
    running = true
    const n = total
    try {
      for (const j of runnableDrafts) upsertJob(await api.queueJob(j.id))
      toast(`${n} ${n === 1 ? 'image' : 'images'} queued`, { action: { label: 'Open queue', run: () => go('queue') } })
    } catch (e) {
      toastError(e)
      refreshJobs()
    } finally {
      running = false
    }
  }
  async function runBatch(batch) {
    try {
      const r = await api.queueBatch(batch.id)
      await refreshJobs()
      toast(`${r.queued} ${r.queued === 1 ? 'job' : 'jobs'} queued${r.skipped ? `, ${r.skipped} skipped` : ''}`, { action: { label: 'Open queue', run: () => go('queue') } })
    } catch (e) {
      toastError(e)
      refreshJobs()
    }
  }
  async function removeBatch(batch) {
    try {
      const r = await api.deleteBatch(batch.id)
      await refreshJobs()
      toast(`${r.deleted} ${r.deleted === 1 ? 'card' : 'cards'} removed`)
    } catch (e) {
      toastError(e)
      refreshJobs()
    }
  }
  async function runOne(job) {
    try {
      upsertJob(await api.queueJob(job.id))
      toast(`${job.count} ${job.count === 1 ? 'image' : 'images'} queued`, { action: { label: 'Open queue', run: () => go('queue') } })
    } catch (e) {
      toastError(e)
      refreshJobs()
    }
  }

  // ----- planning: the request box, live status, re-plan -----
  const replannable = $derived(drafts.filter((j) => j.status !== 'planning' && j.description))
  // Plans on screen: the one being worked on, the ones waiting in the line, and finished ones until they are dismissed or fade
  let tracked = $state([]) // ids of the plans shown, in the order they were first seen
  const shown = $derived(
    tracked
      .map((id) => live.plans[id])
      // a plan the user took out of the line before it started leaves nothing to report
      .filter((p) => p && !(p.status === 'cancelled' && !p.started_at))
      .sort((a, b) => a.id - b.id),
  )
  const inLine = (p) => p.status === 'queued' && !!p.queue_position // waiting behind another plan
  const working = $derived(shown.filter((p) => !inLine(p))) // full status: being worked on, or finished
  const waiting = $derived(shown.filter(inLine).sort((a, b) => a.queue_position - b.queue_position))
  const planning = $derived(activePlans().length > 0)
  let box
  const lastRequests = {} // plan id -> { request, images } of a request that goes back into the box if the plan fails

  function track(id) {
    if (!tracked.includes(id)) tracked.push(id)
  }
  function dismiss(id) {
    tracked = tracked.filter((x) => x !== id)
  }
  function show(p) {
    // the event stream can be ahead of the answer to the request (a plan that fails at once): never go back from done or failed
    if (!live.plans[p.id] || isActivePlan(live.plans[p.id])) live.plans[p.id] = p
    track(p.id)
  }
  async function startPlan(request, workflowId, images) {
    try {
      const p = await api.plan(request, workflowId, images)
      lastRequests[p.id] = { request, images }
      show(p)
      return live.plans[p.id].status !== 'failed' // failed already: the text stays in the box
    } catch (e) {
      toastError(e)
      return false
    }
  }
  /** Re-plan one job, or every job that can be (locked fields stay either way). */
  async function replan(jobs) {
    try {
      const p = jobs.length === 1 ? await api.replanJob(jobs[0].id) : await api.replan(jobs.map((j) => j.id))
      for (const j of jobs) upsertJob({ ...j, status: 'planning' })
      show(p)
    } catch (e) {
      toastError(e)
      refreshJobs()
    }
  }
  // plans that are working or waiting (also ones started in another window or before a reload) are shown
  $effect(() => {
    for (const p of activePlans()) track(p.id)
  })
  // a request that failed goes back into the box, so fixing the cause and pressing Plan again is one step
  $effect(() => {
    for (const p of shown) {
      const sent = lastRequests[p.id]
      if (p.status === 'failed' && p.kind === 'plan' && sent) {
        delete lastRequests[p.id]
        box?.restore(sent.request, sent.images)
      }
    }
  })
  // events normally keep the status current; this catches a missed one for every plan in the line with one request
  $effect(() => {
    if (!planning) return
    const t = setInterval(refreshPlans, 2000)
    return () => clearInterval(t)
  })
  // a finished plan's summary fades after a while; a failed or stopped one stays until dismissed
  const fades = new Map() // plan id -> timer
  $effect(() => {
    for (const p of shown) {
      if (p.status === 'done' && !fades.has(p.id)) fades.set(p.id, setTimeout(() => { fades.delete(p.id); dismiss(p.id) }, 15000))
    }
  })
  onMount(() => () => fades.forEach(clearTimeout))

  async function cancelSide(job) {
    try { upsertJob(await api.cancelJob(job.id)) } catch (e) { toastError(e) }
  }
  async function cancelSideBatch(batch) {
    try { await api.cancelBatch(batch.id); await refreshJobs() } catch (e) { toastError(e) }
  }
  /** Stop a plan that is being worked on (it ends after its current step), or take one out of the line (it never runs). */
  async function stopPlan(p) {
    try { live.plans[p.id] = await api.cancelPlan(p.id) } catch (e) { toastError(e) }
  }
  /** The plan still planning a batch's jobs: the one that last planned them (a re-plan has a newer id than the batch's own), or null. */
  function planOf(batch) {
    const ids = [...batch.jobs.filter((j) => j.status === 'planning').map((j) => j.plan_id), batch.id]
    return ids.find((id) => isActivePlan(live.plans[id])) ?? null
  }
</script>

<main class="page with-side">
  <div class="head"><h1>Plan</h1><span class="spacer"></span><ComfyStatus /></div>

  <div class="col">
    {#if hint}
      <div class="how-hint" out:leave>
        <Icon name="book" />
        <p>New here? Approve a workflow card in the Library, choose that workflow below, describe what you want, and run the jobs the LLM plans on it. <a href="#/how">See how it works</a></p>
        <button class="icon-btn sm" onclick={dismissHint} aria-label="Dismiss this hint"><Icon name="x" /></button>
      </div>
    {/if}
    <RequestBox bind:this={box} {planning} primary={total === 0} onplan={startPlan} />
    <div class="plans">
      {#each working as p (p.id)}
        <div out:leave><PlanStatus plan={p} onclose={() => dismiss(p.id)} oncancel={() => stopPlan(p)} /></div>
      {/each}
      {#each waiting as p (p.id)}
        <div out:leave><QueuedPlan plan={p} oncancel={() => stopPlan(p)} /></div>
      {/each}
    </div>

    {#if drafts.length || adding}
      <div class="section-head">
        <h2>Jobs</h2>
        <InfoTip title="Jobs" text="A job is a workflow plus how many images you want and a prompt for each. Plan makes them from your request on the workflow you chose, or you can add one by hand. Run sends the ready ones to the queue, where ComfyUI makes them one at a time." />
        <span class="spacer"></span>
        {#if replannable.length}
          <button class="btn" onclick={() => replan(replannable)} title="Plan every job again. Everything you locked stays as it is."><Icon name="retry" />Re-plan all</button>
        {/if}
        <button class="btn" id="add-job" onclick={() => (adding ? (adding = false) : openPanel())} aria-expanded={adding}><Icon name={adding ? 'x' : 'plus'} />{adding ? 'Close' : 'Add job by hand'}</button>
        {#if total > 0}
          <button class="btn primary" onclick={runAll} disabled={running}><Icon name="play" />Run {total} {total === 1 ? 'image' : 'images'}</button>
        {/if}
      </div>
    {/if}

    <div class="reveal" class:open={adding} onkeydown={panelKey} role="presentation">
      <div>
        <div class="composer">
          {#if runnable.length}
            <div class="stack" style="gap:10px">
              <div class="pick-head"><h3>Workflow</h3><InfoTip text="The workflow is the recipe ComfyUI follows. Pick one marked ready; each has its own look and settings, described by its card in the Library." label="About workflows" />{#if pickedWf}<span class="picked" title={pickedWf.title}>{pickedWf.title}</span>{/if}</div>
              <WorkflowPicker workflows={runnable} value={pick} onchange={(id) => { if (id !== pick) { pick = id; images = [] } }} />
              {#if notReady}<p class="hint">{notReady} {notReady === 1 ? 'workflow isn’t' : 'workflows aren’t'} ready to run. <a href="#/library">See why in Library</a></p>{/if}
            </div>
            {#if pickedWf?.kind === 'edit'}
              <div class="pick-head"><h3>Input images</h3><InfoTip text="This workflow edits pictures instead of starting from nothing. Drop the image or images it should work from." label="About input images" /></div>
              <div class="drops" style="margin-top:0">
                {#each neededImages as slot, i}
                  <DropZone label={slot.label || `Input image ${i + 1}`} name={images[i] || null} onchange={(n) => (images[i] = n)} />
                {/each}
              </div>
            {/if}
            <div class="bar">
              <div class="stepper" role="group" aria-label="Number of images">
                <button aria-label="Fewer images" disabled={count <= 1} onclick={() => (count = Math.max(1, count - 1))}><Icon name="minus" /></button>
                <input type="number" min="1" max={maxImages} aria-label="Images" bind:value={count} onchange={() => (count = Math.min(Math.max(Math.round(Number(count) || 1), 1), maxImages))} />
                <button aria-label="More images" disabled={count >= maxImages} onclick={() => (count = Math.min(maxImages, count + 1))}><Icon name="plus" /></button>
              </div>
              <span class="muted">{count === 1 ? 'image' : 'images'}</span><InfoTip text="How many images this job makes. Each one gets its own prompt, and you can write them after you create the job." label="About image count" />
              <span class="spacer"></span>
              <button class="btn" onclick={create} disabled={!canCreate}>{busy ? 'Creating' : 'Create job'}</button>
            </div>
          {:else}
            <p class="muted">No workflow is ready to run yet. Import one in the Library first.</p>
            <div class="bar"><a class="btn" href="#/library">Open Library</a></div>
          {/if}
        </div>
      </div>
    </div>

    {#if !live.ready}
      <div class="skeleton" aria-hidden="true"></div>
    {:else if drafts.length}
      <div class="col" style="gap:20px">
        {#each cards as card, i (card.key)}
          {#if card.batch}
            {@const planId = planOf(card)}
            <div class="card-slot" out:leave>
              <BatchCard batch={card} {wfs} oncancelplan={planId != null ? () => stopPlan(live.plans[planId]) : null} onrunall={runBatch} onreplanmany={replan} onremoveall={removeBatch}
                onupdate={upsertJob} onrefresh={refreshJobs} onremove={remove} onduplicate={duplicate} onrun={runOne} onreplan={(j) => replan([j])} />
            </div>
          {:else}
            {@const job = card.job}
            <div class="card-slot" out:leave>
              {#if wfs[job.workflow_id]}
                <JobCard {job} wf={wfs[job.workflow_id]} delay={80 + i * 110} onupdate={upsertJob} onrefresh={refreshJobs} onremove={remove} onduplicate={duplicate} onrun={runOne} onreplan={(j) => replan([j])} />
              {:else if !job.workflow_id}
                <JobCard {job} wf={null} delay={80 + i * 110} onupdate={upsertJob} onrefresh={refreshJobs} onremove={remove} onduplicate={duplicate} onrun={runOne} onreplan={(j) => replan([j])} />
              {:else}
                <div class="skeleton"></div>
              {/if}
            </div>
          {/if}
        {/each}
      </div>
    {:else if !adding}
      <div class="empty fade-in">
        <div class="glyph"><Icon name="plan" /></div>
        <h3>No jobs yet</h3>
        <p>Choose a workflow and describe what you want in the box above, then press Plan. Or build a job yourself: pick a workflow and how many images, then write a prompt for each.</p>
        <button class="btn" onclick={openPanel}><Icon name="plus" />Add job by hand</button>
      </div>
    {/if}
  </div>

  <aside class="side" aria-label="Queue">
    <h2>Queue</h2>
    {#if sideJobs.length}
      <div class="group">
        {#each sideJobs as entry (entry.key)}
          {#if entry.batch}
            <BatchRow batch={entry} oncancel={cancelSideBatch} />
          {:else}
            <QueueRow job={entry.job} oncancel={cancelSide} />
          {/if}
        {/each}
      </div>
      <a class="side-link" href="#/queue">Open queue</a>
    {:else}
      <p class="muted" style="font-size:13px">Nothing running. Jobs you run show up here.</p>
    {/if}
  </aside>
</main>

<style>
  .plans { display: grid; gap: 8px; }
  .plans:empty { display: none; }
</style>
