<script>
  // Input image slot for image-edit workflows: drop a file or click to choose; uploads to ComfyUI.
  import Icon from './Icon.svelte'
  import { api } from './api.js'
  import { previews, toastError } from './store.svelte.js'

  let { label = 'Input image', name = null, onchange } = $props()
  let over = $state(false)
  let busy = $state(false)
  let input

  async function take(file) {
    if (!file) return
    if (!file.type.startsWith('image/')) return toastError(new Error('That file is not an image.'))
    busy = true
    try {
      const { name: stored } = await api.upload(file)
      previews[stored] = URL.createObjectURL(file)
      onchange(stored)
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
  const shown = $derived(name ? name.replace(/^cp_[0-9a-f]{8}_/, '') : '')
</script>

<button
  type="button"
  class="drop"
  class:over
  class:filled={!!name}
  onclick={() => input.click()}
  ondragover={(e) => { e.preventDefault(); over = true }}
  ondragleave={() => (over = false)}
  ondrop={(e) => { e.preventDefault(); over = false; take(e.dataTransfer.files[0]) }}
  disabled={busy}
>
  <span class="pv">
    {#if busy}<span class="spin" style="display:grid"><Icon name="loader" size={20} /></span>
    {:else if name && previews[name]}<img src={previews[name]} alt="" />
    {:else}<Icon name={name ? 'image' : 'upload'} size={20} />{/if}
  </span>
  <span class="tx">
    <b>{name ? shown : label}</b>
    <span>{busy ? 'Uploading to ComfyUI' : name ? 'Click or drop to replace' : 'Drop an image here, or click to choose'}</span>
  </span>
  <input bind:this={input} type="file" accept="image/*" class="sr" tabindex="-1" onchange={(e) => { take(e.currentTarget.files[0]); e.currentTarget.value = '' }} />
</button>
