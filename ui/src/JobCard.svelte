<script>
  import Icon from './Icon.svelte'
  import FieldChip from './FieldChip.svelte'
  import DropZone from './DropZone.svelte'
  import InfoTip from './InfoTip.svelte'
  import PromptList from './PromptList.svelte'
  import WhyLine from './WhyLine.svelte'
  import { api } from './api.js'
  import { live, toast, toastError, workflowTitle, blockingIssues } from './store.svelte.js'
  import { pop, dismissable, autogrow } from './motion.js'

  // onrun(job) queues this job, onreplan(job) plans it again around its locks (both return a promise)
  let { job, wf, delay = 0, onupdate, onrefresh, onremove, onduplicate, onrun, onreplan } = $props()

  const F = $derived(job.fields ?? {})
  const val = (name) => F[name]?.value
  const isLocked = (name) => F[name]?.source === 'locked'
  const slots = $derived(wf?.slots ?? {})
  const hasHires = $derived(!!slots.hires)
  const opt = $derived(live.options)
  const maxImages = $derived(live.config.max_images_per_job)
  const meta = $derived(job.meta ?? {})

  const busy = $derived(job.status === 'planning') // the planner is working on it (a re-plan keeps showing the old values)
  const fresh = $derived(busy && !Object.keys(F).length) // brand new card: nothing planned yet
  const planned = $derived(!!job.plan_id)
  const canReplan = $derived(!!job.description)
  const issues = $derived(job.issues ?? [])
  const blocking = $derived(blockingIssues(job))
  const promptIssues = $derived(issues.filter((i) => i.field === 'prompts'))
  const otherIssues = $derived(issues.filter((i) => i.field !== 'prompts'))
  const issuesFor = (...names) => issues.filter((i) => names.includes(i.field))
  const LABELS = { workflow: 'Workflow', loras: 'LoRAs', width: 'Size', height: 'Size', cfg: 'CFG', steps: 'Steps', sampler: 'Sampler', scheduler: 'Scheduler',
    seed: 'Seed', seed_strategy: 'Seed', hires: 'Hires', hires_steps: 'Hires', hires_denoise: 'Hires', negative: 'Negative', input_images: 'Input image', count: 'Images' }

  let detailsOpen = $state(false)
  let menuOpen = $state(false)

  // ----- saving: one request at a time per card; the backend keeps the fields a request doesn't mention -----
  let chain = Promise.resolve()
  function enqueue(call) {
    chain = chain.then(async () => {
      try {
        const result = await call()
        onupdate(result)
        const n = result?.removed_trigger_words?.prompts // a removed LoRA took its trigger words out of the prompts
        if (n) toast(`Removed its trigger words from ${n} prompt${n === 1 ? '' : 's'}`)
      } catch (e) {
        toastError(e)
        onrefresh()
      }
    })
    return chain
  }
  function save(changes, count) {
    const patch = {}
    for (const [k, c] of Object.entries(changes)) {
      patch[k] = c.raw ?? { value: c.value, source: c.locked ? 'locked' : 'auto', reason: c.locked ? 'set by hand' : (F[k]?.reason ?? '') }
    }
    if (count != null) patch.count = count
    return enqueue(() => api.patchJob(job.id, patch))
  }
  const setField = (name, value, locked = true) => save({ [name]: { value, locked } })
  /** Lock or unlock fields (names the job doesn't have are skipped). */
  function setLock(names, locked) {
    const have = names.filter((n) => n in F)
    if (have.length) enqueue(() => api.setLocks(job.id, { fields: have, locked }))
  }
  const lockedAll = (names) => {
    const have = names.filter((n) => n in F)
    return have.length > 0 && have.every(isLocked)
  }
  const toggle = (names) => setLock(names, !lockedAll(names))

  // ----- chips -----
  const tipFor = (names, auto) => {
    const f = F[names[0]]
    const bad = issuesFor(...names)[0]
    if (bad) return ['Check this', bad.message]
    if (f?.note) return ['Adjusted', f.note]
    if (f?.source === 'locked') return ['Locked by you', f.reason && f.reason !== 'set by hand' ? f.reason : 'Kept when you re-plan']
    return [planned ? 'Chosen by the LLM' : 'Workflow default', f?.reason ? f.reason[0].toUpperCase() + f.reason.slice(1) : auto]
  }
  const flagged = (...names) => issuesFor(...names).length > 0 || names.some((n) => F[n]?.note)
  const num = (v) => (v == null ? 'default' : String(v))
  const loraName = (n) => n.replace(/\.(safetensors|ckpt|pt)$/i, '')
  const loras = $derived(val('loras') ?? [])
  const usable = $derived(opt.loras.filter((n) => !loras.some((l) => l.name === n)))
  const loraEntries = $derived(new Map(live.loras.map((l) => [l.key, l]))) // covers for the chips and the chooser
  const entryOf = (name) => loraEntries.get(name)
  const w = $derived(val('width'))
  const h = $derived(val('height'))

  function saveLora(i, v, lock) {
    setField('loras', loras.map((l, j) => (j === i ? { ...l, weight: v.weight } : l)), lock)
  }
  const removeLora = (i) => setField('loras', loras.filter((_, j) => j !== i), isLocked('loras'))

  // ----- prompts: locked one by one (field.locks); every prompt locked = the whole field is locked -----
  const prompts = $derived(val('prompts') ?? [])
  const plocks = $derived(prompts.map((_, i) => F.prompts?.source === 'locked' || !!F.prompts?.locks?.[i]))
  const emptyAt = $derived(prompts.findIndex((p) => !p.trim()))
  const scene = $derived(meta.scene ?? null)
  const sceneLine = $derived(scene ? [scene.subjects, scene.outfit, scene.pose, scene.setting, scene.lighting].filter(Boolean).join(' · ') : '')
  const sceneRating = $derived(scene?.rating && scene.rating !== 'general' ? scene.rating : '')
  const ratingInPrompts = $derived(!!sceneRating && prompts.some((p) => p.split(',').map((t) => t.trim().toLowerCase()).includes(sceneRating)))
  const gaps = $derived(meta.wildcard_gaps ?? [])
  const sceneTitle = $derived('The concrete scene the prompts were written from. The rating is the tag written into the prompt; edit a prompt to remove it.')
  const promptsChange = (value, locks) => ({ raw: { value, locks, source: 'auto', reason: F.prompts?.reason ?? '' } })
  const savePrompts = (value, locks) => save({ prompts: promptsChange(value, locks) })
  const lockPrompt = (i, locked) => enqueue(() => api.setLocks(job.id, { prompts: [i], locked }))
  function setCount(n) {
    const np = [...prompts], nl = [...plocks]
    while (np.length < n) { np.push(np[np.length - 1] ?? ''); nl.push(false) }
    np.length = nl.length = n
    save({ prompts: promptsChange(np, nl) }, n)
  }

  // ----- workflow -----
  // the workflow is always the user's choice: a job made by Plan says so, and changing it here is another choice of yours
  const wfField = $derived(meta.workflow?.source === 'locked' ? { source: 'locked', reason: meta.workflow.reason ?? '' } : null)
  const choices = $derived(live.workflows.filter((x) => x.runnable || x.id === job.workflow_id))
  async function chooseWorkflow(id) {
    if (!id || id === job.workflow_id) return
    try {
      const updated = await api.patchJob(job.id, { workflow_id: id }) // the re-plan below plans everything else for it
      onupdate(updated)
      await onreplan(updated)
    } catch (e) {
      toastError(e)
      onrefresh()
    }
  }

  // ----- details -----
  const strategy = $derived(val('seed_strategy') ?? 'random')
  let negDraft = $state(null)
  function commitNegative() {
    if (negDraft !== null && negDraft !== (val('negative') ?? '')) setField('negative', negDraft)
    negDraft = null
  }
  const clampNum = (v, lo, hi) => Math.min(Math.max(Number(v), lo), hi)
  function inlineNum(name, e, lo, hi, int) {
    const raw = e.currentTarget.value
    if (raw === '') return (e.currentTarget.value = val(name) ?? '')
    const v = clampNum(raw, lo, hi)
    setField(name, int ? Math.round(v) : v)
  }

  const imagesNeeded = $derived(slots.input_images ?? [])
  const inputImages = $derived(val('input_images') ?? [])
  function setImage(i, name) {
    const next = imagesNeeded.map((_, j) => (j === i ? name : inputImages[j] ?? ''))
    setField('input_images', next, true)
  }

  const canRun = $derived(job.status === 'ready' && !blocking.length && emptyAt < 0 && prompts.length > 0)
  const loraText = $derived(loras.length ? loras.map((l) => `${loraName(l.name)} ${l.weight}`).join(', ') : 'None')
  const lockedPrompts = $derived(plocks.filter(Boolean).length)
</script>

{#snippet lockBtn(names, label)}
  <button class="lock-mini" class:on={lockedAll(names)} aria-pressed={lockedAll(names)} aria-label="{lockedAll(names) ? 'Unlock' : 'Lock'} {label}"
    title={lockedAll(names) ? 'Locked: a re-plan keeps it. Click to unlock.' : 'Lock it so a re-plan keeps it.'} onclick={() => toggle(names)}><Icon name={lockedAll(names) ? 'lock' : 'unlock'} /></button>
{/snippet}

{#snippet why(names, fallback = '')}
  <div class="whys"><WhyLine field={F[names[0]]} {fallback} issues={issuesFor(...names)} /></div>
{/snippet}

<section class="job arrive" class:busy style="animation-delay:{delay}ms">
  {#if busy}<span class="prog indet topbar" aria-hidden="true"><i></i></span>{/if}
  <div class="jh">
    <h3 title={workflowTitle(job.workflow_id)}>{workflowTitle(job.workflow_id)}</h3>
    {#if fresh}
      <span class="chip ghost static">{job.count} {job.count === 1 ? 'image' : 'images'}</span>
    {:else}
      <FieldChip type="count" ghost lockable={false} display="{job.count} {job.count === 1 ? 'image' : 'images'}" value={job.count} min={1} max={maxImages} step={1} onsave={(v) => setCount(v)} />
    {/if}
    {#if !fresh}
      <InfoTip title="Auto and locked" text="Grey chips were chosen by the LLM (or are the workflow's defaults). Mint chips with a lock are yours: click a chip to change it and it locks, and a re-plan leaves it alone. Click the lock to hand it back. The line under each prompt says why it differs from the others." label="About auto and locked values" />
    {/if}
    <span class="spacer"></span>
    {#if busy}
      <span class="working">{fresh ? 'Planning' : 'Re-planning around your locks'}</span>
    {:else}
      {#if blocking.length}<span class="badge draft"><Icon name="alert" />Needs attention</span>{/if}
      {#if canReplan}
        <button class="btn sm" onclick={() => onreplan(job)} title="Plan this job again. Everything you locked stays as it is."><Icon name="retry" />Re-plan</button>
      {/if}
      <button class="btn sm" onclick={() => onrun(job)} disabled={!canRun}><Icon name="play" />Run</button>
      <div class="menu-wrap" use:dismissable={() => (menuOpen = false)}>
        <button class="icon-btn" aria-label="Job actions" aria-haspopup="menu" aria-expanded={menuOpen} onclick={() => (menuOpen = !menuOpen)}><Icon name="more" /></button>
        {#if menuOpen}
          <div class="menu" role="menu" transition:pop>
            <button role="menuitem" onclick={() => { menuOpen = false; onduplicate(job) }}><Icon name="copy" />Duplicate</button>
            <button role="menuitem" class="danger" onclick={() => { menuOpen = false; onremove(job) }}><Icon name="trash" />Delete</button>
          </div>
        {/if}
      </div>
    {/if}
  </div>

  {#if job.description}<p class="desc">{job.description}</p>{/if}
  {#if meta.request}<p class="asked"><span class="asked-label">You asked</span><span class="asked-text">{meta.request}</span></p>{/if}
  {#if job.error && job.status === 'draft'}
    <div class="need"><span class="msg"><Icon name="alert" />{job.error}</span>{#if canReplan}<button class="pick" onclick={() => onreplan(job)}>Re-plan</button>{/if}</div>
  {/if}

  {#if fresh}
    <div class="sk" aria-hidden="true"><i></i><i></i><i></i></div>
  {:else}
    <div class="cardbody" inert={busy}>
      {#if wf}
        <div class="chips">
          {#each loras as l, i (l.name)}
            <FieldChip type="lora" wide display={loraName(l.name)} value={l} {entryOf} locked={isLocked('loras')} flag={flagged('loras')} tip={l.named ? ['Named by you', 'You named this LoRA in the request'] : tipFor(['loras'], 'Suggested by the workflow')}
              onsave={(v, lock) => saveLora(i, v, lock)} onunlock={() => setLock(['loras'], false)} onremove={() => removeLora(i)} />
          {/each}
          {#if loras.length < live.config.max_loras}
            <FieldChip type="lora-add" options={usable} {entryOf} lockable={false} onsave={(v) => setField('loras', [...loras, v], true)} />
          {/if}
        </div>

        <div class="chips">
          {#if slots.width && slots.height}
            <FieldChip type="size" display={w && h ? `${w}×${h}` : 'default'} value={{ width: w, height: h }} locked={isLocked('width')} flag={flagged('width', 'height')} tip={tipFor(['width', 'height'], 'The workflow’s size')}
              onsave={(v, lock) => save({ width: { value: v.width, locked: lock }, height: { value: v.height, locked: lock } })} onunlock={() => setLock(['width', 'height'], false)} />
          {/if}
          {#if slots.size_follows_image}
            <span class="size-follows" title="This workflow edits your image and keeps its size (rounded to 32), so there is no size to set.">Size follows your image</span>
          {/if}
          {#if slots.cfg}
            <FieldChip type="number" label="CFG" display={num(val('cfg'))} value={val('cfg')} min={0} max={30} step={0.1} locked={isLocked('cfg')} flag={flagged('cfg')} tip={tipFor(['cfg'], 'The workflow’s CFG')}
              onsave={(v, lock) => setField('cfg', v, lock)} onunlock={() => setLock(['cfg'], false)} />
          {/if}
          {#if slots.steps}
            <FieldChip type="number" label="Steps" display={num(val('steps'))} value={val('steps')} min={1} max={200} step={1} locked={isLocked('steps')} flag={flagged('steps')} tip={tipFor(['steps'], 'The workflow’s step count')}
              onsave={(v, lock) => setField('steps', v, lock)} onunlock={() => setLock(['steps'], false)} />
          {/if}
          {#if slots.sampler}
            <FieldChip type="select" label="Sampler" display={num(val('sampler'))} value={val('sampler')} options={opt.samplers} locked={isLocked('sampler')} flag={flagged('sampler')} tip={tipFor(['sampler'], 'The workflow’s sampler')}
              onsave={(v, lock) => setField('sampler', v, lock)} onunlock={() => setLock(['sampler'], false)} />
          {/if}
        </div>
      {/if}

      {#if imagesNeeded.length}
        <div class="drops">
          {#each imagesNeeded as slot, i}
            <DropZone label={slot.label || `Input image ${i + 1}`} name={inputImages[i] || null} onchange={(n) => setImage(i, n)} />
          {/each}
        </div>
      {/if}

      {#if emptyAt >= 0}
        <div class="need"><span class="msg"><Icon name="alert" />Image {emptyAt + 1} has no prompt yet. This job is skipped until it does.</span></div>
      {/if}

      {#each otherIssues as issue}
        <div class="need">
          <span class="msg" class:soft={issue.severity !== 'blocking'}><Icon name="alert" />{#if issue.field !== 'job'}<b>{LABELS[issue.field] ?? issue.field}</b>{/if}{issue.message}</span>
          {#if issue.code === 'llm_failed' && canReplan}<button class="pick" onclick={() => onreplan(job)}>Re-plan</button>{/if}
          {#if issue.code === 'no_card'}<a class="pick" href="#/library">Open Library</a><a class="pick" href="#/settings">Open Settings</a>{/if}
        </div>
      {/each}

      {#if prompts.length}
        <PromptList {prompts} locks={plocks} items={F.prompts?.items ?? []} onchange={savePrompts} onlock={lockPrompt} />
      {/if}
      {#if scene}
        <div class="job-scene" title={sceneTitle}>
          {#if sceneRating}<span class="job-scene-rating">{sceneRating}</span>{/if}
          <span class="job-scene-text"><b>Scene</b> {sceneLine}</span>
          {#if sceneRating}<span class="job-scene-hint">{ratingInPrompts ? 'rating tag in the prompt' : 'rating not in the prompt'}</span>{/if}
        </div>
      {/if}
      {#if gaps.length}
        <div class="job-scene" title="None of your enabled wildcard lists has entries for this part of the picture, so the model wrote it. Import or switch on a list that has some to draw from your own.">
          <span class="job-scene-hint">No wildcard list covers {new Intl.ListFormat('en').format(gaps)}; the model wrote {gaps.length === 1 ? 'it' : 'them'}.</span>
        </div>
      {/if}
      {#each promptIssues as issue}
        <div class="need">
          <span class="msg" class:soft={issue.severity !== 'blocking'}><Icon name="alert" />{issue.message}</span>
          {#if issue.code === 'llm_failed' && canReplan}<button class="pick" onclick={() => onreplan(job)}>Re-plan</button>{/if}
        </div>
      {/each}

      {#if wf}
        <div class="details" class:open={detailsOpen}>
          <div>
            <dl class="dl">
              <dt>Workflow</dt>
              <dd>
                <div class="row">
                  {#if canReplan}
                    <span style="flex:1;min-width:200px;max-width:380px">
                      <select class="select" aria-label="Workflow" value={job.workflow_id ?? ''} onchange={(e) => chooseWorkflow(e.currentTarget.value)}>
                        {#each choices as x}<option value={x.id}>{x.title}</option>{/each}
                      </select>
                    </span>
                  {:else}
                    <span>{workflowTitle(job.workflow_id)}</span>
                  {/if}
                </div>
                {#if wfField}<div class="whys"><WhyLine field={wfField} /></div>{/if}
              </dd>

              <dt>LoRAs</dt>
              <dd><div class="row"><span>{loraText}</span>{@render lockBtn(['loras'], 'LoRAs')}</div>{@render why(['loras'], 'Suggested by the workflow')}</dd>

              {#if slots.width && slots.height}
                <dt>Size</dt>
                <dd><div class="row"><span class="mono">{w && h ? `${w}×${h}` : 'default'}</span>{@render lockBtn(['width', 'height'], 'size')}</div>{@render why(['width', 'height'], 'The workflow’s size')}</dd>
              {/if}
              {#if slots.cfg}
                <dt>CFG</dt>
                <dd><div class="row"><span class="mono">{num(val('cfg'))}</span>{@render lockBtn(['cfg'], 'CFG')}</div>{@render why(['cfg'], 'The workflow’s CFG')}</dd>
              {/if}
              {#if slots.steps}
                <dt>Steps</dt>
                <dd><div class="row"><span class="mono">{num(val('steps'))}</span>{@render lockBtn(['steps'], 'steps')}</div>{@render why(['steps'], 'The workflow’s step count')}</dd>
              {/if}
              {#if slots.sampler}
                <dt>Sampler</dt>
                <dd><div class="row"><span class="mono">{num(val('sampler'))}</span>{@render lockBtn(['sampler'], 'sampler')}</div>{@render why(['sampler'], 'The workflow’s sampler')}</dd>
              {/if}
              {#if slots.scheduler}
                <dt>Scheduler<InfoTip text="How noise is removed across the steps. Leave it unless the workflow's card says otherwise." /></dt>
                <dd>
                  <div class="row">
                    <span style="width:200px">
                      {#if opt.schedulers.length}
                        <select class="select" aria-label="Scheduler" value={val('scheduler')} onchange={(e) => setField('scheduler', e.currentTarget.value)}>
                          {#each opt.schedulers.includes(val('scheduler')) ? opt.schedulers : [val('scheduler'), ...opt.schedulers] as o}<option value={o}>{o}</option>{/each}
                        </select>
                      {:else}
                        <span class="mono">{val('scheduler') ?? 'default'}</span>
                      {/if}
                    </span>
                    {@render lockBtn(['scheduler'], 'scheduler')}
                  </div>
                  {@render why(['scheduler'], 'The workflow’s scheduler')}
                </dd>
              {/if}

              <dt>Seed<InfoTip text="The seed is the random starting noise. Random gives each image a new one, Fixed reuses one so you can compare changes, Increment adds one per image." /></dt>
              <dd>
                <div class="row">
                  <span class="seg" role="radiogroup" aria-label="Seed strategy">
                    {#each [['random', 'Random'], ['fixed', 'Fixed'], ['increment', 'Increment']] as [k, l]}
                      <button role="radio" aria-checked={strategy === k} onclick={() => setField('seed_strategy', k)}>{l}</button>
                    {/each}
                  </span>
                  {#if strategy !== 'random'}
                    <input class="field num mini" type="number" min="0" step="1" aria-label="Seed" value={val('seed') ?? 0} onchange={(e) => inlineNum('seed', e, 0, 2 ** 53, true)} />
                  {/if}
                  {@render lockBtn(['seed_strategy', 'seed'], 'seed')}
                </div>
                {@render why(['seed_strategy'], 'New seed per image')}
              </dd>

              {#if hasHires}
                <dt>Hires pass<InfoTip text="Redraws the image larger for more detail, and takes longer. Denoise is how much that second pass may change the image, from 0 to 1." /></dt>
                <dd>
                  <div class="row">
                    <button class="switch" role="switch" aria-checked={!!val('hires')} aria-label="Hires pass" onclick={() => setField('hires', !val('hires'))}></button>
                    {#if val('hires')}
                      <label class="row muted" style="gap:6px">Steps <input class="field num mini" type="number" min="1" max="200" value={val('hires_steps') ?? ''} onchange={(e) => inlineNum('hires_steps', e, 1, 200, true)} /></label>
                      <label class="row muted" style="gap:6px">Denoise <input class="field num mini" type="number" min="0" max="1" step="0.05" value={val('hires_denoise') ?? ''} onchange={(e) => inlineNum('hires_denoise', e, 0, 1, false)} /></label>
                    {:else}
                      <span class="muted">Off</span>
                    {/if}
                    {@render lockBtn(['hires', 'hires_steps', 'hires_denoise'], 'hires settings')}
                  </div>
                  {@render why(['hires'], 'The workflow’s hires setting')}
                </dd>
              {/if}

              <dt>Negative<InfoTip text="What to keep out of the image. It starts from the workflow's own negative, minus any tags you removed in Settings." /></dt>
              <dd>
                <div class="row" style="align-items:flex-start">
                  <textarea class="textarea" rows="2" aria-label="Negative prompt" value={negDraft ?? val('negative') ?? ''} use:autogrow oninput={(e) => (negDraft = e.currentTarget.value)} onblur={commitNegative} placeholder="Nothing to avoid"></textarea>
                  {@render lockBtn(['negative'], 'negative prompt')}
                </div>
                {@render why(['negative'], 'The workflow’s own negative')}
              </dd>

              <dt>Prompts</dt>
              <dd>
                <div class="row"><span>{prompts.length} {prompts.length === 1 ? 'prompt' : 'prompts'}{lockedPrompts ? `, ${lockedPrompts} locked` : ''}</span>{@render lockBtn(['prompts'], 'all prompts')}</div>
                {@render why(['prompts'], '')}
              </dd>
            </dl>
          </div>
        </div>
        <button class="more" aria-expanded={detailsOpen} onclick={() => (detailsOpen = !detailsOpen)}><Icon name="chevron" />Settings and reasons</button>
      {/if}
    </div>
  {/if}
</section>
