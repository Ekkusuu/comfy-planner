<script>
  // A plan waiting in the line, as one row: what it is, how far from its turn, and a button that takes it out of the line.
  import Icon from './Icon.svelte'

  let { plan, oncancel } = $props()

  const n = $derived(plan.job_ids?.length ?? 0)
  const what = $derived(
    plan.kind === 'replan' ? `Re-plan ${n} ${n === 1 ? 'job' : 'jobs'}`
      : plan.kind === 'tweak' ? `Change a job: ${plan.request}`
      : plan.request,
  )
  const ahead = $derived((plan.queue_position ?? 1) - 1) // plans waiting before it, not counting the one being worked on
</script>

<div class="qp">
  <span class="what" title={what}>{what}</span>
  <span class="turn">{ahead < 1 ? 'Next' : `${ahead} ahead`}</span>
  <button class="icon-btn sm" onclick={oncancel} aria-label="Remove from the line: {what}" title="Remove from the line"><Icon name="x" /></button>
</div>

<style>
  .qp { display: flex; align-items: center; gap: 10px; min-height: 40px; padding: 4px 8px 4px 16px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); animation: fadein 0.4s var(--ease) backwards; }
  .what { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-soft); font-size: 13.5px; }
  .turn { flex: none; color: var(--muted); font-size: 12.5px; }
</style>
