<script module>
  // The newest loaded frame outlives the component, so a batch that hops between jobs, or a return to the Queue page,
  // keeps its picture instead of going blank.
  let held = null // { scope, url, ar }
</script>

<script>
  // The large live picture of the running row on the Queue page: the newest step preview of the image being generated.
  // A frame replaces the last one only after it has loaded (no flicker). Between images the last frame stays, dimmed.
  import Icon from './Icon.svelte'
  import { api } from './api.js'
  import { live } from './store.svelte.js'

  // jobId: the job now running (null between jobs of a batch). scope: what the picture belongs to, a job or a whole batch.
  let { jobId, scope } = $props()

  let frame = $state(held?.scope === scope ? held : null)
  $effect(() => {
    const number = jobId != null ? live.preview[jobId] : null
    if (!number) return
    const url = api.previewUrl(jobId, number)
    const img = new Image()
    let stale = false
    img.onload = () => {
      if (stale) return
      frame = held = { scope, url, ar: img.naturalWidth / img.naturalHeight }
    }
    img.src = url
    return () => { stale = true }
  })
  const between = $derived(!!frame && !live.preview[jobId])
  const frac = $derived(between ? 0 : (live.progress[jobId]?.frac ?? 0))
</script>

<div class="stage-pic" class:between style="--ar:{frame?.ar ?? 0.75}">
  {#if frame}
    <img src={frame.url} alt="Live preview of the image being generated" decoding="sync" />
  {:else}
    <span class="stage-ph"><Icon name="image" /></span>
  {/if}
  <i class="stage-bar" style="transform:scaleX({frac})" aria-hidden="true"></i>
</div>
