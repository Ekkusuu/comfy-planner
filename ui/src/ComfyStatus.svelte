<script>
  import Icon from './Icon.svelte'
  import { live, comfyUp, startComfy, ui } from './store.svelte.js'

  const known = $derived(live.health !== null)
</script>

<span class="status">
  {#if !known}
    <span class="dot wait"></span>Checking ComfyUI
  {:else if comfyUp()}
    <span class="dot"></span>ComfyUI ready
  {:else}
    <span class="dot off"></span>ComfyUI offline
    <button class="btn sm" onclick={startComfy} disabled={ui.starting}>
      {#if ui.starting}<span class="spin" style="display:grid"><Icon name="loader" /></span>Starting{:else}Start{/if}
    </button>
  {/if}
</span>
