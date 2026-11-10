<script>
  // The LoRA card: build or re-generate it, review the diff, edit the fields, approve.
  import { untrack } from 'svelte'
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import CardLoadFailed from './CardLoadFailed.svelte'
  import CardDiff from './CardDiff.svelte'
  import ListField from './ListField.svelte'
  import RangeField from './RangeField.svelte'
  import LoraCover from './LoraCover.svelte'
  import TagEditor from './TagEditor.svelte'
  import { api } from './api.js'
  import { live, toast, toastError, refreshLoras, changeLoraTags } from './store.svelte.js'
  import { isBuilding, buildText, clone, same } from './cards.js'
  import { tagsInUse } from './loraCatalogue.js'

  let { entry } = $props() // list entry: { key, state, size, ... }

  // The editor belongs to one LoRA file. `entry` is replaced whenever the Library list refreshes (a Rescan, a cover
  // arriving), so everything below depends on the file name only: a refresh must not reload the card or drop edits.
  const key = $derived(entry.key)
  let data = $state(null) // GET /api/cards/loras/<file>
  let draft = $state(null)
  let busy = $state(false)
  let loadError = $state(null) // the message when the card could not be loaded
  let loads = 0 // counts loads: only the latest one may fill the editor

  const build = $derived(live.cardBuilds[`lora:${entry.key}`])
  const building = $derived(isBuilding(build))
  const dirty = $derived(!!draft && !!data?.card && !same(draft, data.card))
  const hasCard = $derived(!!data?.card)
  const canBuild = $derived(entry.state !== 'removed' && entry.state !== 'missing')

  async function load(file = key) {
    const mine = ++loads
    loadError = null
    try {
      const out = await api.card('loras', file)
      if (mine !== loads) return // another LoRA was opened, or a newer load started
      data = out
      draft = out.card ? clone(out.card) : null
    } catch (e) {
      if (mine === loads) loadError = e?.message || String(e)
    }
  }
  const retry = () => {
    data = draft = null
    load()
  }
  $effect(() => {
    const file = key
    untrack(() => {
      data = draft = null
      load(file)
    })
  })
  let lastStage = untrack(() => build?.stage) // a build that finished before the editor opened is already what load() shows
  $effect(() => {
    const stage = build?.stage
    if (stage !== lastStage) {
      lastStage = stage
      if (stage === 'done' || stage === 'failed') load()
    }
  })

  async function run(fn, done) {
    busy = true
    try {
      const out = await fn()
      if (out && out.kind) {
        data = out
        draft = out.card ? clone(out.card) : null
        refreshLoras() // the Library list and the request box show the card's status too
      }
      if (done) toast(done)
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
  async function startBuild() {
    try {
      live.cardBuilds[`lora:${entry.key}`] = await api.buildCard('loras', entry.key)
    } catch (e) {
      toastError(e)
    }
  }
  const save = () => run(() => api.saveCard('loras', entry.key, draft), 'Saved')
  const approve = () => run(async () => {
    if (dirty) await api.saveCard('loras', entry.key, draft)
    return api.approveCard('loras', entry.key)
  }, 'Approved')
  const accept = () => run(() => api.acceptCard('loras', entry.key), 'Using the re-generated card')
  const reject = () => run(() => api.rejectCard('loras', entry.key))
  const discard = () => (draft = clone(data.card))

  // ----- cover and the user's tags: kept apart from the card, so they never change its status or get lost on a re-generate -----
  const COVER_FROM = { user: 'Your choice', file: 'A preview file next to the LoRA', civitai: 'The preview picture from Civitai' }
  let upload = $state() // the hidden file input
  let tagBusy = $state(false)
  const tagList = $derived(entry.user_tags ?? [])
  async function changeTags(add, remove) {
    tagBusy = true
    try {
      await changeLoraTags([entry.key], add, remove)
    } catch (e) {
      toastError(e)
    } finally {
      tagBusy = false
    }
  }
  async function coverFrom(file) {
    if (!file) return
    busy = true
    try {
      await api.uploadLoraCover(entry.key, file)
      await refreshLoras()
      toast('Cover updated')
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
      upload.value = ''
    }
  }
  async function coverAutomatic() {
    busy = true
    try {
      await api.resetLoraCover(entry.key)
      await refreshLoras()
      toast('The cover is automatic again')
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
  const grow = (node) => {
    const fit = () => { node.style.height = 'auto'; node.style.height = node.scrollHeight + 2 + 'px' }
    fit(); node.addEventListener('input', fit)
    return { destroy: () => node.removeEventListener('input', fit) }
  }
</script>

<svelte:boundary onerror={(e) => console.error(e)}>
{#if loadError}
  <CardLoadFailed message={loadError} onretry={retry} />
{:else if !data}
  <p class="muted">Loading the card…</p>
{:else}
  <section class="mine" aria-label="Cover and tags">
    <div class="mine-cover">
      <span class="mine-img"><LoraCover entry={entry} big /></span>
      <div class="mine-side">
        <span class="muted">{entry.cover?.source ? COVER_FROM[entry.cover.source] : 'No cover yet'}</span>
        <span class="mine-btns">
          <button type="button" class="btn sm" onclick={() => upload.click()} disabled={busy}><Icon name="upload" />Upload</button>
          {#if entry.cover?.chosen}<button type="button" class="btn sm" onclick={coverAutomatic} disabled={busy}>Automatic</button>{/if}
          <InfoTip text="Upload a picture, or open an image in the Gallery and press Use as LoRA cover. Automatic goes back to a preview file next to the LoRA, then the Civitai picture." label="About covers" />
        </span>
        <input bind:this={upload} class="sr" type="file" accept="image/png,image/jpeg,image/webp" tabindex="-1" aria-label="Choose a cover picture" onchange={(e) => coverFrom(e.currentTarget.files?.[0])} />
      </div>
    </div>
    <div class="mine-tags">
      <span class="lb"><label for="l-mytags">Your tags</label><InfoTip text="Your own words for finding this LoRA: a place, a mood, a project. You can search and filter by them, and the planner reads them when it looks for a LoRA. They are kept when the card is re-generated." /></span>
      <TagEditor id="l-mytags" tags={tagList} all={tagsInUse(live.loras)} busy={tagBusy} onadd={(list) => changeTags(list, [])} onremove={(t) => changeTags([], [t])} label="Your tags on this LoRA" />
    </div>
  </section>

  <div class="bar">
    {#if hasCard}
      <span class="badge" class:draft={data.status === 'draft'} class:ok={data.status === 'approved'}>{data.status === 'approved' ? 'Approved' : 'Draft'}</span>
    {:else}
      <span class="badge">No card</span>
    {/if}
    <InfoTip text="A draft is a card the planner ignores until you approve it (unless Settings allows drafts). Re-generate writes a fresh card next to the current one so you can compare." label="Draft or approved?" />
    {#if entry.state === 'changed'}<span class="badge draft" title="The file is different from the one this card was built from">File changed</span><InfoTip text="The LoRA file was replaced or edited since this card was built. Re-generate the card, or check it still describes the file." label="About a changed file" />{/if}
    {#if entry.state === 'removed' || entry.state === 'missing'}<span class="badge err">{entry.state === 'removed' ? 'File removed' : 'File not found'}</span>{/if}
    {#if building}
      <span class="status"><span class="spin" style="display:grid"><Icon name="loader" /></span>{buildText(build)}</span>
    {:else if build?.stage === 'failed'}
      <span class="status err"><Icon name="alert" />{build.message}</span>
    {/if}
    <span class="spacer"></span>
    <button class="btn" class:primary={!hasCard} onclick={startBuild} disabled={building || busy || !canBuild}>
      {#if hasCard}<Icon name="retry" />Re-generate{:else}<Icon name="spark" />Build card{/if}
    </button>
    {#if hasCard}
      <button class="btn" class:primary={data.status === 'draft' && !data.pending} onclick={approve} disabled={busy || data.status === 'approved'}>
        <Icon name="check" />{data.status === 'approved' ? 'Approved' : 'Approve'}
      </button>
    {/if}
  </div>

  {#if !hasCard}
    <div class="empty compact">
      <h3>No card yet</h3>
      <p>The app reads the file’s header (not the weights) and asks Civitai about it, then the LLM writes a draft for you to review.</p>
    </div>
  {:else}
    {#if data.pending}
      <section class="cf-sec pending">
        <div class="bar">
          <h4>A re-generated card is ready</h4>
          <span class="spacer"></span>
          <button class="btn" onclick={reject} disabled={busy}>Keep current</button>
          <button class="btn primary" onclick={accept} disabled={busy}>Use re-generated</button>
        </div>
        <CardDiff before={data.card} after={data.pending} />
      </section>
    {/if}
    {#if draft.metadata_only}
      <p class="notice"><Icon name="alert" />{draft.notice || 'Built from the file’s own metadata only.'}</p>
    {/if}
    {#if draft.needs_attention.length}
      <p class="notice"><Icon name="alert" />The LLM couldn’t write: {draft.needs_attention.join(', ')}. Fill them in, or re-generate.</p>
    {/if}

    <div class="cf">
      <section class="cf-sec">
        <div class="cf-grid">
          <span class="lb"><label for="l-what">What it does</label><InfoTip text="What this LoRA changes in an image, in plain words. The planner reads it to decide when to use it." /></span>
          <textarea id="l-what" class="textarea" rows="2" bind:value={draft.what_it_does} use:grow></textarea>
          <span class="lb"><label for="l-kind">Kind</label><InfoTip text="What it is for: a character, a style, a concept, clothing or a pose." /></span>
          <select id="l-kind" class="select" style="max-width:220px" bind:value={draft.kind}>
            {#each live.config.lora_kinds as k}<option value={k}>{k}</option>{/each}
          </select>
          <span class="lb"><label for="l-family">Family</label><InfoTip text="The base model it was trained for. It only works with workflows of the same family." /></span>
          <select id="l-family" class="select" style="max-width:220px" bind:value={draft.family}>
            {#each live.config.card_families as f}<option value={f}>{f}</option>{/each}
          </select>
          <span class="lb"><label for="l-trig">Trigger words</label><InfoTip text="Words that must be in the prompt for the LoRA to take effect. The planner adds them automatically." /></span>
          <ListField id="l-trig" mode="commas" bind:value={draft.trigger_words} mono placeholder="Words that must be in the prompt" />
          <span class="lb">The LoRA sets the look<InfoTip text="On: this LoRA paints its own colours, light and rendering, so on a job that uses it the planner offers no lighting or colour idea and leaves out generic look tags (cinematic lighting, depth of field, bokeh). Light and colours you write in your request stay. On for style LoRAs by default; turn it off for a LoRA that works well with lighting." /></span>
          <button class="switch" role="switch" aria-checked={draft.sets_look} aria-label="The LoRA sets the look" onclick={() => (draft.sets_look = !draft.sets_look)}></button>
          <span class="lb"><label for="l-noneg">Remove from negative</label><InfoTip text="Negative tags this LoRA works better without. On a job that uses this LoRA they are taken out of the negative prompt, and the job card says so. Whole tags only; weights are ignored. Words you typed in your request, or a locked negative, stay." /></span>
          <ListField id="l-noneg" mode="commas" bind:value={draft.remove_from_negative} mono placeholder="For example: painting, cartoon" />
          <span class="lb">Weight<InfoTip text="How strongly the LoRA is applied. Recommended is the default; going outside min and max usually looks wrong." /></span><RangeField bind:value={draft.weight} />
          <span class="lb"><label for="l-notes">Usage notes</label><InfoTip text="Tips for using it well, one per line. The prompt writer reads them." /></span>
          <ListField id="l-notes" bind:value={draft.usage_notes} rows={3} placeholder="One note per line" />
          <span class="lb"><label for="l-tags">Top training tags</label><InfoTip text="The tags that appeared most in its training images. A hint of what it was taught." /></span>
          <ListField id="l-tags" mode="commas" bind:value={draft.tags} mono />
        </div>
      </section>

      <section class="cf-sec">
        <h4 class="hx">Read from the file<InfoTip text="Facts taken from the LoRA file itself. The hash identifies the exact file, so the card is flagged if the file changes." /></h4>
        <dl class="dl facts">
          <dt>File</dt><dd class="mono">{draft.file}</dd>
          <dt>SHA256</dt><dd class="mono">{draft.sha256 ? draft.sha256.slice(0, 16) + '…' : 'unknown'}</dd>
          {#if draft.civitai}
            <dt>Civitai</dt>
            <dd>
              <a class="ext" href={draft.civitai.url} target="_blank" rel="noopener noreferrer">{draft.civitai.name || 'Open on Civitai'}{draft.civitai.version_name ? ` · ${draft.civitai.version_name}` : ''}<Icon name="external" /></a>
              {#if draft.civitai.base_model}<span class="muted"> · {draft.civitai.base_model}</span>{/if}
            </dd>
          {/if}
        </dl>
        {#if draft.style_tags.length}
          <details class="fold">
            <summary>Style tags<span class="muted"> · {draft.style_tags.length} found in the training data</span></summary>
            <p class="tagcloud mono">{draft.style_tags.join('  ')}</p>
          </details>
        {/if}
        {#if draft.civitai?.example_prompts?.length}
          <details class="fold">
            <summary>Example prompts from Civitai<span class="muted"> · {draft.civitai.example_prompts.length}</span></summary>
            {#each draft.civitai.example_prompts as p}<pre class="notes">{p}</pre>{/each}
          </details>
        {/if}
      </section>
    </div>

    {#if dirty}
      <div class="savebar">
        <span class="muted">You have unsaved edits</span>
        <span class="spacer"></span>
        <button class="btn" onclick={discard} disabled={busy}>Discard</button>
        <button class="btn primary" onclick={save} disabled={busy}>Save edits</button>
      </div>
    {/if}
  {/if}
{/if}
{#snippet failed(error, reset)}
  <CardLoadFailed message={error?.message?.startsWith('https://svelte.dev') ? '' : error?.message} onretry={() => { reset(); retry() }} />
{/snippet}
</svelte:boundary>

<style>
  .mine { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 20px; align-items: start; padding-bottom: 14px; border-bottom: 1px solid var(--line); }
  .mine-cover { display: flex; gap: 14px; align-items: flex-start; }
  .mine-img { position: relative; flex: none; width: 120px; aspect-ratio: 4 / 3; border-radius: var(--r-xl); overflow: hidden; box-shadow: 0 0 0 1px var(--line2); }
  .mine-side { display: grid; gap: 8px; font-size: 12.5px; max-width: 150px; }
  .mine-btns { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .mine-tags { display: grid; gap: 6px; min-width: 0; }
  @media (max-width: 640px) { .mine { grid-template-columns: minmax(0, 1fr); } }
  .bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .bar h4 { margin: 0; font-size: 13.5px; font-weight: 600; }
  .status { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12.5px; }
  .status.err { color: var(--red); }
  .notice { display: flex; gap: 8px; align-items: center; color: var(--amber); font-size: 13px; }
  .pending { padding: 14px; border: 1px solid var(--mint-line); border-radius: var(--r-xl); background: var(--mint-dim); }
  .facts { padding: 0; grid-template-columns: 110px minmax(0, 1fr); gap: 8px 16px; }
  .facts dd { overflow-wrap: anywhere; color: var(--text-soft); }
  .ext { display: inline-flex; align-items: center; gap: 6px; color: var(--mint); }
  .ext:hover { text-decoration: underline; }
  .tagcloud { margin: 8px 0 0; line-height: 1.8; color: var(--text-soft); overflow-wrap: anywhere; }
  .notes { margin: 8px 0 0; padding: 12px; max-height: 240px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font: 12.5px/1.55 var(--mono); color: var(--text-soft); background: var(--bg); border-radius: var(--r-md); }
</style>
