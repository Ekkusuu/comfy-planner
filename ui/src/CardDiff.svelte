<script>
  // What changed between the current card and a re-generated one: only the fields that differ.
  import { diffCards } from './cards.js'
  let { before, after } = $props()
  const changes = $derived(diffCards(before, after))
</script>

{#if changes.length}
  <div class="diff">
    <div class="diff-head"><span>Field</span><span>Current</span><span>Re-generated</span></div>
    {#each changes as c}
      <div class="diff-row">
        <span class="lb">{c.label}</span>
        <span class="old" class:none={!c.before}>{c.before || 'Empty'}</span>
        <span class="new" class:none={!c.after}>{c.after || 'Empty'}</span>
      </div>
    {/each}
  </div>
{:else}
  <p class="muted" style="font-size:13px">The re-generated card is the same as the current one.</p>
{/if}

<style>
  .diff { border: 1px solid var(--line); border-radius: var(--r-xl); overflow: hidden; font-size: 13px; }
  .diff-head, .diff-row { display: grid; grid-template-columns: 130px minmax(0, 1fr) minmax(0, 1fr); gap: 14px; padding: 9px 12px; }
  .diff-head { color: var(--muted); font-size: 12px; font-weight: 500; background: var(--bg); }
  .diff-row { border-top: 1px solid var(--line); align-items: start; }
  .lb { color: var(--muted); }
  .old, .new { white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.5; border-radius: var(--r-sm); padding: 2px 6px; margin: -2px -6px; }
  .old { color: var(--muted); background: var(--red-dim); }
  .new { color: var(--text); background: var(--mint-dim); }
  .none { color: var(--faint); font-style: italic; }
  @media (max-width: 760px) {
    .diff-head { display: none; }
    .diff-row { grid-template-columns: 1fr; gap: 6px; }
  }
</style>
