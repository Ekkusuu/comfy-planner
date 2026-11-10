<script>
  // A LoRA's cover picture, or an icon for its kind when it has none. Fills its parent (which must be positioned).
  import Icon from './Icon.svelte'

  let { entry, big = false } = $props() // a /api/cards/loras entry
  const ICONS = { style: 'palette', character: 'user', concept: 'bulb', clothing: 'shirt', pose: 'figure', other: 'box' }
  let failed = $state('') // the address that didn't load
  const url = $derived(entry?.cover?.url ?? null)
</script>

{#if url && failed !== url}
  <img class="lcv-img" src={url} alt="" loading="lazy" decoding="async" draggable="false" onerror={() => (failed = url)} />
{:else}
  <span class="lcv-ph" class:big><Icon name={ICONS[entry?.card?.kind] ?? 'box'} /></span>
{/if}

<style>
  .lcv-img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; object-position: 50% 22%; animation: fadein 0.45s var(--ease) backwards; }
  .lcv-ph { position: absolute; inset: 0; display: grid; place-items: center; color: var(--faint); background: radial-gradient(120% 90% at 50% 0%, rgba(255, 255, 255, 0.06), transparent 70%), var(--raised); }
  .lcv-ph :global(svg.i) { width: 55%; height: 55%; max-width: 24px; max-height: 24px; }
  .lcv-ph.big :global(svg.i) { max-width: 32px; max-height: 32px; }
</style>
