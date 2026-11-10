<script>
  // Shown where a card editor could not load or draw its card: what went wrong, and a way to try again.
  import Icon from './Icon.svelte'

  let { message = '', onretry } = $props()
</script>

<div class="clf" role="alert">
  <p><Icon name="alert" />Couldn’t show this card.{#if message} <span class="muted">{message}</span>{/if}</p>
  <button type="button" class="btn sm" onclick={onretry}><Icon name="retry" />Retry</button>
</div>

<style>
  .clf { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; padding: 12px 14px; border: 1px solid var(--red-line); border-radius: var(--r-xl); background: var(--red-dim); }
  .clf p { margin: 0; display: flex; align-items: center; flex-wrap: wrap; gap: 6px; color: var(--red); font-size: 13px; overflow-wrap: anywhere; }
  .clf p :global(.muted) { color: var(--text-soft); }
</style>
