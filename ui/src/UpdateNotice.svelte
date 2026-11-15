<script>
  // The opt-in update check at start: when `check_updates_at_start` is on, ask once after the app has loaded and show a small
  // dismissible notice if a newer version exists. Nothing is asked when the setting is off (the default).
  import { live } from './store.svelte.js'
  import { api } from './api.js'
  import Icon from './Icon.svelte'
  import { pop } from './motion.js'

  let found = $state(null) // {latest, url} when a newer version exists and was not dismissed
  let asked = false

  // A version you dismissed is not shown again; the next newer one is.
  const dismissedVersion = () => {
    try { return localStorage.getItem('dismissedUpdate') } catch { return null }
  }
  function dismiss() {
    try { localStorage.setItem('dismissedUpdate', found.latest) } catch {}
    found = null
  }

  $effect(() => {
    if (!live.ready || asked || !live.config.check_updates_at_start) return
    asked = true
    api.updateCheck().then((r) => {
      if (r.newer && r.latest !== dismissedVersion()) found = { latest: r.latest, url: r.url }
    }).catch(() => {}) // a failed check at start is not worth a message; the Settings button shows errors
  })
</script>

{#if found}
  <div class="update" role="status" transition:pop={{ y: 8, scale: 0.96 }}>
    <Icon name="info" />
    <span>Version {found.latest} is available.</span>
    <a class="act" href={found.url} target="_blank" rel="noopener noreferrer">Open the release page</a>
    <button class="icon-btn sm" onclick={dismiss} aria-label="Dismiss"><Icon name="x" /></button>
  </div>
{/if}

<style>
  .update { position: fixed; left: 16px; bottom: 24px; z-index: 90; display: flex; align-items: center; gap: 10px; max-width: calc(100vw - 32px); padding: 8px 8px 8px 14px; border-radius: var(--r-xl); background: var(--float); border: 1px solid var(--line2); box-shadow: var(--shadow-pop); font-size: 13.5px; }
  .update :global(svg.i) { color: var(--mint); flex: none; }
  .act { color: var(--mint); font-weight: 500; padding: 3px 8px; border-radius: var(--r-sm); }
  .act:hover { background: var(--mint-dim); }
</style>
