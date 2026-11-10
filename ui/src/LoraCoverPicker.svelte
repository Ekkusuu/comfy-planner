<script>
  // In the Gallery viewer: choose which LoRA this image becomes the cover of. LoRAs the image's job used come first.
  import { onMount } from 'svelte'
  import Icon from './Icon.svelte'
  import LoraCover from './LoraCover.svelte'
  import { api } from './api.js'
  import { live, toast, toastError, refreshLoras } from './store.svelte.js'
  import { loraName, matchesQuery, searchText } from './loraCatalogue.js'

  let { imageId, used = [], ondone } = $props() // used: LoRA file names from the image's recipe

  const SHOWN = 40
  let q = $state('')
  let busy = $state(false)
  onMount(() => {
    if (!live.loras.length) refreshLoras()
  })

  const byKey = $derived(new Map(live.loras.map((l) => [l.key, l])))
  const first = $derived([...new Set(used)].map((n) => byKey.get(n)).filter(Boolean))
  const rest = $derived(live.loras.filter((l) => !used.includes(l.key) && matchesQuery(searchText(l), q)))

  async function pick(l) {
    busy = true
    try {
      await api.chooseLoraCover(l.key, imageId)
      await refreshLoras()
      toast(`Cover for ${loraName(l.key)} updated`)
      ondone()
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
</script>

{#snippet row(l)}
  <li>
    <button type="button" class="lp-row" disabled={busy} onclick={() => pick(l)}>
      <span class="lp-img"><LoraCover entry={l} /></span>
      <span class="lp-name" title={l.key}>{loraName(l.key)}</span>
      {#if l.cover?.chosen}<span class="lp-note">has your cover</span>{/if}
    </button>
  </li>
{/snippet}

<div class="lp" role="group" aria-label="Choose a LoRA">
  {#if !live.loras.length}
    <p class="muted lp-empty">No LoRAs found. Add some in the Library first.</p>
  {:else}
    {#if first.length}
      <h4 class="lp-h">Used for this image</h4>
      <ul class="lp-list">{#each first as l (l.key)}{@render row(l)}{/each}</ul>
    {/if}
    {#if first.length < live.loras.length}
      <h4 class="lp-h">{first.length ? 'Other LoRAs' : 'Use as the cover of'}</h4>
      {#if live.loras.length > 8}
        <div class="lp-search"><Icon name="search" /><input class="field" aria-label="Find a LoRA" placeholder="Find a LoRA" bind:value={q} autocomplete="off" spellcheck="false" /></div>
      {/if}
      <ul class="lp-list">{#each rest.slice(0, SHOWN) as l (l.key)}{@render row(l)}{/each}</ul>
      {#if rest.length > SHOWN}<p class="muted lp-empty">{rest.length - SHOWN} more. Type to narrow the list.</p>
      {:else if !rest.length}<p class="muted lp-empty">No LoRA matches.</p>{/if}
    {/if}
  {/if}
</div>

<style>
  .lp { flex-basis: 100%; display: grid; gap: 6px; padding: 10px; margin-top: 4px; border: 1px solid var(--line2); border-radius: var(--r-xl); background: var(--raised); }
  .lp-h { margin: 2px 2px 0; font-size: 12px; font-weight: 500; color: var(--muted); }
  .lp-list { list-style: none; margin: 0; padding: 0; display: grid; gap: 2px; max-height: 200px; overflow-y: auto; }
  .lp-row { width: 100%; display: flex; align-items: center; gap: 10px; padding: 4px 6px; border-radius: var(--r-md); text-align: left; }
  .lp-row:hover:not(:disabled) { background: var(--float); }
  .lp-row:disabled { opacity: 0.5; }
  .lp-img { position: relative; flex: none; width: 34px; height: 34px; border-radius: 6px; overflow: hidden; box-shadow: 0 0 0 1px var(--line2); }
  .lp-name { flex: 1; min-width: 0; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lp-note { flex: none; color: var(--muted); font-size: 11.5px; }
  .lp-search { position: relative; display: flex; align-items: center; }
  .lp-search :global(svg.i) { position: absolute; left: 10px; color: var(--muted); pointer-events: none; }
  .lp-search .field { padding-left: 32px; height: 30px; font-size: 13px; }
  .lp-empty { font-size: 12.5px; padding: 2px; }
</style>
