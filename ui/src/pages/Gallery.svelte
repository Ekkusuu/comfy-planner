<script>
  import { tick } from 'svelte'
  import Icon from '../Icon.svelte'
  import InfoTip from '../InfoTip.svelte'
  import { api } from '../api.js'
  import { live, route, go, toast, toastError, prefill, removeImages, refreshWorkflows } from '../store.svelte.js'
  import LoraCoverPicker from '../LoraCoverPicker.svelte'
  import { pop, fade, collapse } from '../motion.js'

  const jobFilter = $derived(route.query.job ? Number(route.query.job) : null)
  const groupKey = $derived(route.query.group || null) // an open folder: a batch ("b12") or a hand-made job with several images ("j5")

  // What is on screen comes from the API and is loaded again when images finish or are deleted: the top level (single images
  // and folders), one folder, or - from a Queue link - the images of one job. There are no other filters.
  const mode = $derived(groupKey ? `g${groupKey}` : jobFilter ? `j${jobFilter}` : 'top')
  let entries = $state([]) // top level: {type: 'image', ...image} or {type: 'group', key, title, request, count, cover_image_id, cover_ratio}
  let imageTotal = $state(0)
  let folder = $state(null) // {key, title, request, count, images}
  let flat = $state([]) // the images of one job
  let loaded = $state(false)
  let shownMode = ''
  let loadId = 0
  async function load(m) {
    const id = ++loadId
    try {
      if (groupKey) folder = await api.galleryGroup(groupKey)
      else if (jobFilter) flat = await api.images(500, 0, jobFilter)
      else {
        const r = await api.gallery(300)
        entries = r.entries
        imageTotal = r.images
      }
    } catch (e) {
      if (groupKey) folder = null // gone (deleted, or a stale link)
      else toastError(e)
    }
    if (id === loadId && m === mode) loaded = true
  }
  $effect(() => {
    const m = mode
    live.images // images finished or were deleted
    if (m !== shownMode) {
      shownMode = m
      loaded = false
      folder = null
    }
    load(m)
  })
  const items = $derived(groupKey ? (folder?.images ?? []) : jobFilter ? flat : entries.filter((e) => e.type === 'image'))
  const shown = $derived(groupKey || jobFilter ? items : entries)
  const count = $derived(groupKey ? (folder?.count ?? 0) : jobFilter ? flat.length : imageTotal)
  /** Drop deleted images from what is on screen now, before the reload brings the same news. */
  function forget(ids) {
    const gone = new Set(ids)
    entries = entries.filter((e) => !(e.type === 'image' && gone.has(e.id)))
    flat = flat.filter((i) => !gone.has(i.id))
    if (folder) folder = { ...folder, images: folder.images.filter((i) => !gone.has(i.id)), count: folder.images.filter((i) => !gone.has(i.id)).length }
    imageTotal = Math.max(0, imageTotal - ids.length)
    removeImages(ids)
  }

  // Aspect ratios and pixel sizes, learned as images load (recipes can lack a size). Thumbnails give the
  // ratio only; the real size comes from the full image in the viewer.
  let ratios = $state({})
  let dims = $state({})
  const ratio = (im) => (im.recipe.width && im.recipe.height ? im.recipe.width / im.recipe.height : (ratios[im.id] ?? 0.8))
  const sizeOf = (im) => (im.recipe.width && im.recipe.height ? [im.recipe.width, im.recipe.height] : dims[im.id] ? [dims[im.id].w, dims[im.id].h] : null)
  const onThumb = (im, e) => (ratios[im.id] = e.currentTarget.naturalWidth / e.currentTarget.naturalHeight)
  const onFull = (im, e) => (dims[im.id] = { w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })

  // ----- viewer -----
  let sel = $state(null) // image id
  let opener = null
  let closeBtn = $state()
  const idx = $derived(items.findIndex((i) => i.id === sel))
  const cur = $derived(idx >= 0 ? items[idx] : null)
  const r = $derived(cur?.recipe ?? {})
  // what you typed on the Plan page, for images from planned jobs
  const asked = $derived(cur ? live.jobs.find((j) => j.id === cur.job_id)?.meta?.request ?? '' : '')

  async function openImage(im, e) {
    opener = e?.currentTarget ?? document.activeElement
    sel = im.id
    await tick()
    closeBtn?.focus()
  }
  function close() {
    sel = null
    opener?.focus?.()
  }
  const step = (d) => {
    const n = items[idx + d]
    if (n) sel = n.id
  }
  function onkey(e) {
    if (sel === null) {
      if (e.key === 'Escape' && folderConfirm) folderConfirm = false
      else if (e.key === 'Escape' && selecting) endSelect()
      return
    }
    if (e.key === 'Escape') pickingLora ? (pickingLora = false) : confirming ? (confirming = false) : close()
    else if (e.target.closest?.('input, textarea')) return // typing in the LoRA finder: Delete and the arrow keys are for the text
    else if (e.key === 'Delete') confirming ? removeCurrent() : (confirming = true)
    else if (e.key === 'ArrowLeft') step(-1)
    else if (e.key === 'ArrowRight') step(1)
    else if (e.key === 'Home') step(-idx)
    else if (e.key === 'End') step(items.length - 1 - idx)
    else if (e.key === 'Tab') {
      const f = [...document.querySelectorAll('.viewer button:not(:disabled), .viewer [href]')]
      if (!f.length) return
      const first = f[0], last = f[f.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
    }
  }
  $effect(() => {
    if (sel !== null && idx < 0) sel = null // filtered away
  })
  // the wheel over the picture steps through images; one step per notch, then a short pause so a spin doesn't skip ahead
  let wheelAt = 0
  function onwheel(e) {
    if (e.ctrlKey || Math.abs(e.deltaY) < 4) return
    e.preventDefault()
    if (e.timeStamp - wheelAt < 220) return
    wheelAt = e.timeStamp
    step(e.deltaY > 0 ? 1 : -1)
  }
  // warm the cache for the neighbours so arrow keys don't wait on the network
  $effect(() => {
    for (const n of [items[idx - 1], items[idx + 1]]) if (n) new Image().src = api.imageUrl(n.id)
  })

  // ----- delete: one image in the viewer, several in select mode -----
  let confirming = $state(false) // viewer: inline "Delete this image?"
  let selecting = $state(false)
  let picked = $state([]) // image ids
  let pickedFolders = $state([]) // folder keys, on the top level only
  const foldersPicked = $derived(entries.filter((e) => e.type === 'group' && pickedFolders.includes(e.key)))
  const pickedImages = $derived(picked.length + foldersPicked.reduce((sum, f) => sum + f.count, 0)) // what deleting them removes
  const anyPicked = $derived(picked.length + pickedFolders.length > 0)
  const countOf = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
  /** "2 folders and 3 images (27 images in all)", or just "3 images". */
  const pickedText = $derived.by(() => {
    if (!foldersPicked.length) return countOf(picked.length, 'image')
    const what = picked.length ? `${countOf(foldersPicked.length, 'folder')} and ${countOf(picked.length, 'image')}` : countOf(foldersPicked.length, 'folder')
    return `${what} (${countOf(pickedImages, 'image')} in all)`
  })
  let bulkConfirm = $state(false)
  let busy = $state(false)
  $effect(() => {
    sel
    confirming = false
  })

  async function removeCurrent() {
    if (!cur || busy) return
    const gone = cur.id, next = items[idx + 1] ?? items[idx - 1]
    busy = true
    try {
      await api.deleteImage(gone)
      sel = next?.id ?? null
      if (!next) opener?.focus?.()
      forget([gone])
      toast('Image deleted')
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
      confirming = false
    }
  }
  // ----- delete a whole folder (asks first, like the images do) -----
  let folderConfirm = $state(false)
  async function removeFolder() {
    if (!folder || busy) return
    const ids = folder.images.map((i) => i.id)
    busy = true
    try {
      await api.deleteGalleryGroup(groupKey)
      removeImages(ids)
      toast(ids.length === 1 ? 'Image deleted' : `${ids.length} images deleted`)
      go('gallery')
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
      folderConfirm = false
    }
  }
  function endSelect() {
    selecting = false
    picked = []
    pickedFolders = []
    bulkConfirm = false
  }
  const toggleSelect = () => (selecting ? endSelect() : (selecting = true))
  function toggleTile(im) {
    picked = picked.includes(im.id) ? picked.filter((i) => i !== im.id) : [...picked, im.id]
    bulkConfirm = false
  }
  function toggleFolder(group) {
    pickedFolders = pickedFolders.includes(group.key) ? pickedFolders.filter((k) => k !== group.key) : [...pickedFolders, group.key]
    bulkConfirm = false
  }
  /** Delete a folder and return the ids of its images (the shared image list has to drop them too). */
  async function deleteFolder(key) {
    const { images } = await api.galleryGroup(key)
    await api.deleteGalleryGroup(key)
    return images.map((i) => i.id)
  }
  async function removePicked() {
    busy = true
    const folderKeys = [...pickedFolders]
    const [imageResults, folderResults] = await Promise.all([
      Promise.allSettled(picked.map((id) => api.deleteImage(id))),
      Promise.allSettled(folderKeys.map(deleteFolder)),
    ])
    const gone = picked.filter((_, i) => imageResults[i].status === 'fulfilled')
    const goneFolders = folderKeys.filter((_, i) => folderResults[i].status === 'fulfilled')
    const goneIds = [...gone, ...folderResults.flatMap((x) => (x.status === 'fulfilled' ? x.value : []))]
    const failed = [...imageResults, ...folderResults].find((x) => x.status === 'rejected')
    busy = false
    entries = entries.filter((e) => !(e.type === 'group' && goneFolders.includes(e.key)))
    forget(goneIds)
    if (failed) {
      toastError(failed.reason)
      picked = picked.filter((id) => !gone.includes(id))
      pickedFolders = pickedFolders.filter((k) => !goneFolders.includes(k))
      bulkConfirm = false
    } else {
      toast(goneIds.length === 1 ? 'Image deleted' : `${goneIds.length} images deleted`)
      endSelect()
    }
  }

  // ----- workflow cover -----
  const coverOf = $derived(cur ? live.workflows.find((w) => w.id === (cur.workflow_id ?? r.workflow_id)) : null)
  const isCover = $derived(!!coverOf?.cover_pinned && coverOf.cover_image_id === cur?.id)
  async function makeCover() {
    try {
      await api.setCover(coverOf.id, cur.id)
      await refreshWorkflows()
      toast(`Cover for ${coverOf.title} updated`)
    } catch (e) {
      toastError(e)
    }
  }

  // ----- LoRA cover -----
  let pickingLora = $state(false)
  $effect(() => {
    cur?.id
    pickingLora = false
  })

  async function copyRecipe() {
    const text = JSON.stringify(r, null, 2)
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const t = document.createElement('textarea')
      t.value = text
      document.body.append(t)
      t.select()
      document.execCommand('copy')
      t.remove()
    }
    toast('Recipe copied')
  }
  function useAsJob() {
    prefill.recipe = { ...r, workflow_id: cur.workflow_id ?? r.workflow_id } // the job's workflow now; the recipe keeps the one that made the image
    sel = null
    go('plan')
  }
  const when = (s) => (s ? new Date(s).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '')
  const rows = $derived.by(() => {
    if (!cur) return []
    const sz = sizeOf(cur)
    const out = [['Size', sz ? `${sz[0]}×${sz[1]}` : null], ['Steps', r.steps], ['CFG', r.cfg], ['Sampler', r.sampler], ['Scheduler', r.scheduler], ['Seed', r.seed]]
    if (r.hires) out.push(['Hires', `${r.hires_steps ?? '?'} steps, denoise ${r.hires_denoise ?? '?'}`])
    return out.filter(([, v]) => v != null)
  })
</script>

<svelte:window onkeydown={onkey} />

<main class="page">
  <div class="head">
    {#if groupKey || jobFilter}
      <button class="btn sm" onclick={() => go('gallery')} aria-label="Back to all images"><Icon name="chevleft" />All images</button>
    {/if}
    <h1>Gallery</h1>
    {#if count}<span class="muted">{selecting && anyPicked ? `${pickedImages} of ${count} selected` : count}</span>{/if}
    {#if jobFilter}
      <button class="btn sm" onclick={() => go('gallery')} aria-label="Show all images">Job {jobFilter}<Icon name="x" /></button>
    {/if}
    <span class="spacer"></span>
    {#if selecting && anyPicked}
      {#if bulkConfirm}
        <span class="ask">Delete {pickedText}?</span>
        <button class="btn sm" onclick={() => (bulkConfirm = false)} disabled={busy}>Cancel</button>
        <button class="btn sm danger armed" onclick={removePicked} disabled={busy}><Icon name="trash" />Delete</button>
      {:else}
        <button class="btn sm danger" onclick={() => (bulkConfirm = true)}><Icon name="trash" />Delete {pickedImages}</button>
      {/if}
    {/if}
    {#if shown.length}
      <InfoTip text="Pick images and folders, then delete them in one go. Picking a folder takes all of its images. It asks first." label="About select" />
      <button class="btn sm" onclick={toggleSelect} aria-pressed={selecting}>{selecting ? 'Done' : 'Select'}</button>
    {/if}
  </div>

  {#if groupKey && folder}
    <section class="gfolder" aria-label="Folder">
      <div class="gfolder-text">
        <h2 class="gfolder-label">{groupKey.startsWith('b') ? 'Batch' : 'Job'} · {folder.count} {folder.count === 1 ? 'image' : 'images'}</h2>
        <p class="gfolder-request">{folder.request}</p>
      </div>
      <div class="gfolder-acts">
        {#if folderConfirm}
          <span class="ask">Delete {folder.count === 1 ? 'this image' : `these ${folder.count} images`}?</span>
          <button class="btn sm" onclick={() => (folderConfirm = false)} disabled={busy}>Cancel</button>
          <button class="btn sm danger armed" onclick={removeFolder} disabled={busy}><Icon name="trash" />Delete</button>
        {:else if folder.count}
          <InfoTip text="Deletes every image in this folder, with its recipe. It asks first. The jobs stay in the Queue list." label="About deleting a folder" />
          <button class="btn sm danger" onclick={() => (folderConfirm = true)}><Icon name="trash" />Delete folder</button>
        {/if}
      </div>
    </section>
  {/if}

  {#if !live.ready || !loaded}
    <div class="skeleton" aria-hidden="true"></div>
  {:else if !shown.length}
    <div class="empty fade-in">
      <div class="glyph"><Icon name="gallery" /></div>
      <h3>{groupKey ? 'No images in this folder' : jobFilter ? 'No finished images for this job' : 'No images yet'}</h3>
      <p>{groupKey ? 'It is empty or no longer exists.' : jobFilter ? 'Images appear here as they finish.' : 'Finished images land here with the full recipe that made them, so you can reproduce or build on any of them. A request that gives several images becomes one folder.'}</p>
      {#if groupKey || jobFilter}
        <button class="btn" onclick={() => go('gallery')}>Show all images</button>
      {:else}
        <a class="btn primary" href="#/plan"><Icon name="plan" />Go to Plan</a>
      {/if}
    </div>
  {:else}
    <div class="grid">
      {#each shown as im (im.type === 'group' ? im.key : im.id)}
        {#if im.type === 'group'}
          {@const far = im.cover_ratio ?? ratios[`c${im.key}`] ?? 0.8}
          {@const onFolder = pickedFolders.includes(im.key)}
          <button class="tile folder" class:picked={onFolder} style="--ar:{far};--w:{Math.round(far * 220)}" out:collapse
            onclick={() => (selecting ? toggleFolder(im) : go('gallery', { group: im.key }))}
            aria-pressed={selecting ? onFolder : undefined}
            aria-label="{selecting ? 'Select' : 'Open'} folder, {im.count} images: {im.title}">
            <img src={api.thumbUrl(im.cover_image_id)} alt="" loading="lazy" decoding="async" onload={(e) => (ratios[`c${im.key}`] = e.currentTarget.naturalWidth / e.currentTarget.naturalHeight)} />
            <span class="gshade"></span>
            <span class="gcount"><Icon name="image" />{im.count}</span>
            <span class="gtitle">{im.title}</span>
            {#if selecting}<span class="mark" class:on={onFolder}><Icon name="check" /></span>{/if}
          </button>
        {:else}
          {@const ar = ratio(im)}
          {@const on = picked.includes(im.id)}
          <button class="tile" class:picked={on} style="--ar:{ar};--w:{Math.round(ar * 220)}" out:collapse
            onclick={(e) => (selecting ? toggleTile(im) : openImage(im, e))}
            aria-pressed={selecting ? on : undefined}
            aria-label="{selecting ? 'Select' : 'Open'} image {im.id}: {im.recipe.prompt?.slice(0, 80) ?? ''}">
            <img src={api.thumbUrl(im.id)} alt="" loading="lazy" decoding="async" onload={(e) => onThumb(im, e)} />
            {#if selecting}<span class="mark" class:on><Icon name="check" /></span>{/if}
          </button>
        {/if}
      {/each}
    </div>
  {/if}
</main>

{#if cur}
  <div class="viewer" role="dialog" aria-modal="true" aria-label="Image {cur.id}" transition:fade={{ duration: 220 }}>
    <button class="scrim" tabindex="-1" aria-label="Close" onclick={close}></button>
    <div class="vframe" transition:pop={{ y: 10, scale: 0.98, duration: 320 }}>
      <div class="stage" onwheel={onwheel}>
        {#key cur.id}<img class="big fade-in" src={api.imageUrl(cur.id)} alt={r.prompt ?? 'Generated image'} onload={(e) => onFull(cur, e)} />{/key}
        <button class="nav-btn l" onclick={() => step(-1)} disabled={idx <= 0} aria-label="Previous image"><Icon name="chevleft" /></button>
        <button class="nav-btn r" onclick={() => step(1)} disabled={idx >= items.length - 1} aria-label="Next image"><Icon name="chevright" /></button>
      </div>
      <aside class="recipe">
        <div class="rh">
          <div style="min-width:0;flex:1">
            <h2>{r.workflow_title ?? 'Image'}</h2>
            <p class="muted">{when(cur.finished_at)} · job {cur.job_id}, image {cur.idx}</p>
          </div>
          <button class="icon-btn" bind:this={closeBtn} onclick={close} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div class="rbody">
          {#if asked}<section><h4>You asked</h4><p class="prompt asked">{asked}</p></section>{/if}
          <section><h4>Prompt</h4><p class="prompt">{r.prompt}</p></section>
          {#if r.prompt_template && r.prompt_template !== r.prompt}<section><h4>Template</h4><p class="prompt dim">{r.prompt_template}</p></section>{/if}
          {#if r.negative}<section><h4>Negative</h4><p class="prompt dim">{r.negative}</p></section>{/if}
          {#if r.loras?.length}
            <section><h4>LoRAs</h4>
              <div class="tags">{#each r.loras as l}<span class="chip">{l.name.replace(/\.(safetensors|ckpt|pt)$/i, '')} <span class="mono muted">{l.weight}</span></span>{/each}</div></section>
          {/if}
          <section><h4>Settings</h4>
            <dl class="dl tight">{#each rows as [k, v]}<dt>{k}</dt><dd class="mono">{v}</dd>{/each}</dl></section>
        </div>
        <div class="ract">
          <button class="btn" onclick={copyRecipe}><Icon name="copy" />Copy recipe</button>
          <InfoTip text="Copies everything used to make this image (prompt, seed, size, LoRAs and the rest) as text, to keep or share." label="About copy recipe" />
          <button class="btn primary" onclick={useAsJob}><Icon name="plus" />Use as new job</button>
          <InfoTip text="Makes a new job on the Plan page with this image's workflow and settings, so you can change something and run it again." label="About use as new job" />
          <div class="manage">
            {#if confirming}
              <span class="ask">Delete this image?</span>
              <span class="spacer"></span>
              <button class="btn sm" onclick={() => (confirming = false)} disabled={busy}>Cancel</button>
              <button class="btn sm danger armed" onclick={removeCurrent} disabled={busy}><Icon name="trash" />Delete</button>
            {:else}
              {#if coverOf}
                <button class="btn sm" onclick={makeCover} disabled={isCover}><Icon name={isCover ? 'check' : 'image'} />{isCover ? 'Workflow cover' : 'Use as cover'}</button>
                <InfoTip text="Shows this image as the thumbnail for its workflow in the Library and the workflow picker. Without one, the newest image is used." label="About workflow covers" />
              {/if}
              <button class="btn sm" onclick={() => (pickingLora = !pickingLora)} aria-expanded={pickingLora}><Icon name="image" />Use as LoRA cover</button>
              <InfoTip text="Shows this image as the thumbnail of a LoRA in the Library and on job cards. The LoRAs this image was made with are listed first. In the LoRA's editor you can go back to the automatic cover." label="About LoRA covers" />
              <span class="spacer"></span>
              <InfoTip text="Deletes the image file and its recipe from this app. It asks first, and the Del key works too." label="About delete" />
              <button class="btn sm danger" onclick={() => (confirming = true)} title="Delete (Del)"><Icon name="trash" />Delete</button>
              {#if pickingLora}<LoraCoverPicker imageId={cur.id} used={(r.loras ?? []).map((l) => l.name)} ondone={() => (pickingLora = false)} />{/if}
            {/if}
          </div>
        </div>
      </aside>
    </div>
  </div>
{/if}

<style>
  .grid { display: flex; flex-wrap: wrap; gap: 8px; }
  .grid::after { content: ''; flex-grow: 1000000; }
  .tile { position: relative; display: block; flex: calc(var(--ar) * 100) 1 calc(var(--w) * 1px); aspect-ratio: var(--ar); min-width: 0; max-width: 100%; border-radius: var(--r-xl); overflow: hidden; background: var(--surface); outline-offset: 2px; }
  .tile img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; transition: transform 0.6s var(--ease), opacity 0.4s var(--ease), border-radius 0.3s var(--ease); }
  .tile:hover img { transform: scale(1.03); }
  .tile.picked img, .tile.picked:hover img { transform: scale(0.94); border-radius: var(--r-md); }
  .tile::after { content: ''; position: absolute; inset: 0; border-radius: inherit; box-shadow: inset 0 0 0 1px var(--line); pointer-events: none; transition: box-shadow 0.25s var(--ease); }
  .tile:hover::after, .tile:focus-visible::after { box-shadow: inset 0 0 0 1px var(--mint-line); }
  .tile.picked::after, .tile.picked:hover::after { box-shadow: inset 0 0 0 2px var(--mint); }
  /* a folder: the cover with the request on it, and cards stacked behind it */
  .tile.folder { margin: 11px 11px 0 0; box-shadow: 5px -5px 0 -1px var(--raised), 5px -5px 0 0 var(--line2), 10px -10px 0 -2px var(--surface), 10px -10px 0 -1px var(--line); }
  .gshade { position: absolute; inset: 0; background: linear-gradient(to top, rgba(8, 8, 10, 0.86) 0%, rgba(8, 8, 10, 0.3) 42%, rgba(8, 8, 10, 0) 68%); pointer-events: none; }
  .gtitle { position: absolute; left: 12px; right: 12px; bottom: 10px; z-index: 1; color: var(--text); font-size: 13px; font-weight: 500; line-height: 1.35; text-align: left; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
  .gcount { position: absolute; top: 10px; right: 10px; z-index: 1; height: 22px; padding: 0 8px; border-radius: 99px; background: rgba(10, 10, 11, 0.62); color: var(--text); font-size: 12px; font-weight: 500; display: inline-flex; align-items: center; gap: 5px; }
  .gcount :global(svg.i) { width: 12px; height: 12px; }
  .gfolder { display: flex; align-items: flex-start; gap: 16px; padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--r-2xl); background: var(--surface); }
  .gfolder-text { min-width: 0; flex: 1; display: grid; gap: 6px; }
  .gfolder-label { font-size: 12px; font-weight: 500; color: var(--muted); }
  .gfolder-request { color: var(--text-soft); font-size: 14px; line-height: 1.55; max-height: 9em; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; user-select: text; }
  .gfolder-acts { display: flex; align-items: center; gap: 8px; flex: none; flex-wrap: wrap; justify-content: flex-end; }
  .mark { position: absolute; top: 10px; left: 10px; z-index: 1; width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center; background: rgba(10, 10, 11, 0.6); box-shadow: inset 0 0 0 1.5px rgba(255, 255, 255, 0.55); color: transparent; transition: background 0.2s var(--ease), box-shadow 0.2s var(--ease), color 0.2s var(--ease); }
  .mark :global(svg.i) { width: 13px; height: 13px; stroke-width: 2.2; }
  .mark.on { background: var(--mint); box-shadow: none; color: var(--mint-ink); }
  .ask { color: var(--text-soft); font-size: 13px; }

  .viewer { position: fixed; inset: 0; z-index: 60; display: grid; place-items: center; padding: 20px; }
  .scrim { position: absolute; inset: 0; background: rgba(5, 5, 6, 0.82); cursor: default; }
  .vframe { position: relative; width: min(1320px, 100%); height: min(860px, 100%); display: grid; grid-template-columns: minmax(0, 1fr) 360px; border-radius: var(--r-2xl); background: var(--surface); border: 1px solid var(--line2); box-shadow: var(--shadow-overlay); overflow: hidden; }
  .stage { position: relative; background: var(--bg); min-height: 0; }
  .big { position: absolute; inset: 20px; width: calc(100% - 40px); height: calc(100% - 40px); object-fit: contain; border-radius: var(--r-md); }
  .nav-btn { position: absolute; top: 50%; width: 36px; height: 36px; margin-top: -18px; border-radius: 50%; background: rgba(26, 26, 29, 0.85); border: 1px solid var(--line2); display: grid; place-items: center; color: var(--text); transition: background 0.2s var(--ease), opacity 0.2s var(--ease); }
  .nav-btn:hover:not(:disabled) { background: var(--float); }
  .nav-btn:disabled { opacity: 0; pointer-events: none; }
  .nav-btn.l { left: 14px; }
  .nav-btn.r { right: 14px; }
  .recipe { display: grid; grid-template-columns: minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr) auto; border-left: 1px solid var(--line); min-height: 0; }
  .rh { display: flex; align-items: flex-start; gap: 8px; padding: 16px 12px 12px 18px; }
  .rh h2 { font-size: 15px; font-weight: 600; letter-spacing: -0.005em; }
  .rh p { font-size: 12.5px; }
  .rbody { overflow: auto; padding: 4px 18px 16px; display: grid; gap: 18px; align-content: start; }
  .rbody h4 { margin: 0 0 6px; font-size: 12px; font-weight: 500; color: var(--muted); }
  .prompt { color: var(--text-soft); font-size: 13.5px; line-height: 1.55; overflow-wrap: anywhere; user-select: text; }
  .prompt.dim { color: var(--muted); }
  .tags { display: flex; flex-wrap: wrap; gap: 6px; }
  .dl.tight { grid-template-columns: 90px 1fr; gap: 6px 12px; padding: 0; }
  .dl.tight dt { padding: 0; }
  .ract { display: flex; align-items: center; gap: 6px; padding: 14px 18px; border-top: 1px solid var(--line); flex-wrap: wrap; }
  .ract > .btn { flex: 1 1 0; min-width: 0; padding-inline: 10px; white-space: nowrap; }
  .manage { flex-basis: 100%; display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-height: 28px; margin-top: 4px; }

  @media (max-width: 900px) {
    .viewer { padding: 0; }
    .vframe { grid-template-columns: 1fr; grid-template-rows: minmax(0, 1fr) minmax(0, 46%); height: 100%; border-radius: 0; border: 0; }
    .recipe { border-left: 0; border-top: 1px solid var(--line); }
    .big { inset: 12px; width: calc(100% - 24px); height: calc(100% - 24px); }
  }
</style>
