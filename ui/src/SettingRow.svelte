<script>
  // One setting: label and info tip on the left, the control on the right, then an error or a hint.
  import InfoTip from './InfoTip.svelte'

  let { label, tip, id, changed = false, error = '', hint = '', children } = $props()
</script>

<div class="row" class:changed class:bad={error}>
  <div class="lab">
    <label for={id}>{label}</label><InfoTip text={tip} label="About {label.toLowerCase()}" />
  </div>
  <div class="ctl">
    {@render children()}
    {#if error}<p class="err" role="alert">{error}</p>{:else if hint}<p class="hint">{hint}</p>{/if}
  </div>
</div>

<style>
  .row { position: relative; display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 6px 20px; padding: 12px 16px; border-top: 1px solid var(--line); align-items: start; }
  .row:first-child { border-top: 0; }
  .lab { padding-top: 6px; min-height: 32px; line-height: 20px; color: var(--muted); font-size: 13px; }
  .lab :global(.wrap) { margin-left: 6px; }
  .lab label { color: var(--text-soft); }
  .ctl { min-width: 0; display: grid; gap: 6px; justify-items: start; align-content: start; }
  .ctl > :global(input.field), .ctl > :global(textarea), .ctl > :global(.tags) { width: 100%; }
  .ctl > :global(.switch) { margin: 6px 0; }
  .row.changed::before { content: ''; position: absolute; left: 6px; top: 24px; width: 4px; height: 4px; border-radius: 50%; background: var(--mint); }
  .hint, .err { font-size: 12.5px; line-height: 1.45; max-width: 60ch; }
  .hint { color: var(--muted); }
  .err { color: var(--red); }
  .row.bad :global(.field), .row.bad :global(.select) { border-color: var(--red-line); }
  @media (max-width: 700px) { .row { grid-template-columns: 1fr; } }
</style>
