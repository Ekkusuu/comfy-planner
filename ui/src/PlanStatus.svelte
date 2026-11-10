<script>
  // Live status of a planning run: queued -> waiting -> loading -> planning -> done | failed | cancelled.
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import { activePlans } from './store.svelte.js'

  let { plan, onclose, oncancel } = $props()

  const STAGES = [
    ['queued', 'Queued'],
    ['waiting', 'Waiting for the image'],
    ['loading', 'Loading the LLM'],
    ['planning', 'Planning'],
    ['done', 'Done'],
  ]
  const at = $derived(Math.max(0, STAGES.findIndex(([k]) => k === plan.status)))
  const failed = $derived(plan.status === 'failed')
  const cancelled = $derived(plan.status === 'cancelled')
  const done = $derived(plan.status === 'done' || cancelled)
  const active = $derived(!failed && !done)
  const stopping = $derived(active && /cancelling/i.test(plan.message || ''))

  let tick = $state(Date.now())
  $effect(() => {
    if (!active) return
    const t = setInterval(() => (tick = Date.now()), 1000)
    return () => clearInterval(t)
  })
  // the clock counts from when the plan left the line; time spent waiting behind other plans is not counted, and a plan not started has no clock
  const clock = $derived(!!plan.started_at || plan.status !== 'queued')
  const started = $derived(Date.parse(plan.started_at ?? plan.created_at))
  const secs = $derived(active ? Math.max(0, Math.round((tick - started) / 1000)) : Math.round(((Date.parse(plan.finished_at) || tick) - started) / 1000))
  const elapsed = $derived(secs >= 60 ? `${Math.floor(secs / 60)} min ${secs % 60} s` : `${secs} s`)

  const what = $derived(plan.kind === 'replan' ? 'Re-planning' : plan.kind === 'tweak' ? 'Changing the job' : 'Planning your request')
  const n = $derived(plan.job_ids?.length ?? 0)
  const doneText = $derived(
    plan.kind === 'tweak' ? plan.message
      : `${plan.kind === 'replan' ? 'Re-planned' : 'Planned'} ${n} ${n === 1 ? 'job' : 'jobs'}`,
  )
  // what the plan says about itself after "Done" (who got images, which LoRAs were left out and why)
  const said = $derived(plan.kind === 'tweak' ? '' : (plan.message || '').replace(/^Done\.?\s*/, '').trim())
  const lines = $derived({
    queued: ['Getting ready.', ''],
    waiting: ['Waiting for the current image to finish.', 'Image generation pauses for planning as soon as it does.'],
    loading: ['Loading the LLM onto the graphics card.', 'Image generation is paused until planning is done.'],
    planning: [plan.message || 'Reading the request', 'Image generation stays paused while the LLM works.'],
    done: [`${doneText} in ${elapsed}.`, said || (activePlans().length ? 'The LLM stays loaded for the next plan in the line.' : 'The LLM is unloaded and the queue continues.')],
    cancelled: [`${plan.message} (after ${elapsed}).`, 'What was planned is kept. The jobs not reached are drafts: use Re-plan failed on the batch to plan them later.'],
  }[plan.status] ?? ['', ''])
  const needsCards = $derived(failed && /approved? (workflow )?card|draft cards/i.test(plan.error || plan.message || ''))
</script>

<section class="ps" class:failed class:done aria-live="polite">
  {#if failed}
    <div class="fail">
      <Icon name="alert" />
      <div class="tx">
        <b>Planning failed</b>
        <span>{plan.error || plan.message}</span>
        {#if needsCards}
          <span class="acts"><a class="btn sm" href="#/library">Open Library</a><a class="btn sm" href="#/settings">Open Settings</a></span>
        {/if}
      </div>
      <button class="icon-btn sm" onclick={onclose} aria-label="Dismiss"><Icon name="x" /></button>
    </div>
  {:else}
    <div class="top">
      <b>{cancelled ? 'Planning stopped' : done ? doneText : what}</b>
      <InfoTip title="Why generation pauses" text="The graphics card can hold the LLM or ComfyUI, not both. The queue waits for the current image, ComfyUI's models are unloaded, the LLM plans, and it is unloaded again before generation continues." label="About pausing" />
      <span class="spacer"></span>
      {#if clock}<span class="time mono">{elapsed}</span>{/if}
      {#if active && oncancel && plan.status === 'planning'}<button class="btn sm" onclick={oncancel} disabled={stopping} title="Stop after the step the LLM is working on. What is planned stays."><Icon name="x" />{stopping ? 'Stopping' : 'Stop planning'}</button>{/if}
      {#if done}<button class="icon-btn sm" onclick={onclose} aria-label="Dismiss"><Icon name="x" /></button>{/if}
    </div>
    <ol class="track" aria-label="Planning stages">
      {#each STAGES as [key, label], i}
        <li class:past={i < at || done} class:now={i === at && active} aria-current={i === at ? 'step' : undefined}>
          <span class="pt"></span><span class="lb">{label}</span>
          {#if i < STAGES.length - 1}<span class="bar"><i></i></span>{/if}
        </li>
      {/each}
    </ol>
    <p class="msg">{lines[0]}</p>
    {#if lines[1]}<p class="sub">{lines[1]}</p>{/if}
  {/if}
</section>

<style>
  .ps { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); padding: 14px 16px; display: grid; gap: 10px; animation: fadein 0.4s var(--ease) backwards; }
  .ps.done { border-color: var(--mint-line); }
  .ps.failed { background: var(--red-dim); border-color: var(--red-line); }
  .top { display: flex; align-items: center; gap: 6px; min-height: 28px; }
  .top b { font-size: 14px; font-weight: 600; }
  .time { color: var(--muted); }
  .track { list-style: none; margin: 2px 0 0; padding: 0; display: flex; align-items: center; }
  .track li { display: flex; align-items: center; gap: 8px; color: var(--faint); font-size: 12.5px; white-space: nowrap; transition: color 0.4s var(--ease); }
  .track li:not(:last-child) { flex: 1; min-width: 0; }
  .pt { width: 9px; height: 9px; border-radius: 50%; border: 1.5px solid var(--faint); flex: none; transition: background 0.4s var(--ease), border-color 0.4s var(--ease); }
  .bar { flex: 1; min-width: 14px; height: 1px; margin: 0 8px; background: var(--line2); position: relative; overflow: hidden; }
  .bar i { position: absolute; inset: 0; background: var(--mint); transform: scaleX(0); transform-origin: left; transition: transform 0.7s var(--ease); }
  .past { color: var(--text-soft); }
  .past .pt { background: var(--mint); border-color: var(--mint); }
  .past .bar i { transform: scaleX(1); }
  .now { color: var(--text); }
  .now .pt { border-color: var(--mint); background: var(--mint-dim); animation: beat 1.6s var(--ease) infinite; }
  @keyframes beat { 0% { box-shadow: 0 0 0 0 rgba(110, 231, 183, 0.45); } 70%, 100% { box-shadow: 0 0 0 7px rgba(110, 231, 183, 0); } }
  .msg { color: var(--text); font-size: 13.5px; }
  .sub { color: var(--muted); font-size: 12.5px; margin-top: -6px; }
  .fail { display: flex; align-items: flex-start; gap: 12px; }
  .fail > :global(svg.i) { color: var(--red); margin-top: 3px; }
  .tx { flex: 1; min-width: 0; display: grid; gap: 4px; }
  .tx b { font-weight: 600; font-size: 14px; }
  .tx span { color: var(--text-soft); font-size: 13.5px; line-height: 1.5; }
  .acts { display: flex !important; gap: 8px; margin-top: 6px; flex-wrap: wrap; }
  @media (max-width: 760px) { .track li .lb { display: none; } .track li.now .lb, .track li.past:nth-last-child(2) .lb { display: inline; } }
</style>
