<script>
  // Library → Wildcards: the lists imported from a Forge-style wildcard folder. Folders group the lists; each list has an
  // on/off switch, a role (the way to correct what the app read from its entries), a line saying which parts of a picture it feeds
  // and how sure that is, and a preview of its entries.
  import { onMount, untrack } from 'svelte'
  import Icon from './Icon.svelte'
  import InfoTip from './InfoTip.svelte'
  import DeleteDialog from './DeleteDialog.svelte'
  import { api } from './api.js'
  import { live, toast, toastError } from './store.svelte.js'

  const DEFAULT_PATH = '' // no default folder: the user types the folder with Forge-style wildcard .txt files

  let data = $state(null) // GET /api/wildcards
  let path = $state(DEFAULT_PATH)
  let importing = $state(false) // the folder form is open
  let busy = $state(false)
  let openFolders = $state({}) // folder -> expanded
  let openList = $state(null) // list name whose entries are shown
  let entries = $state({}) // list name -> all entries (loaded when opened)
  let failed = $state('')
  let deleting = $state(null) // the list whose delete dialog is open

  const lists = $derived(data?.lists ?? [])
  const folders = $derived.by(() => {
    const out = new Map()
    for (const l of lists) {
      const key = l.folder || '(top level)'
      if (!out.has(key)) out.set(key, { key, folder: l.folder, lists: [] })
      out.get(key).lists.push(l)
    }
    return [...out.values()]
  })
  const total = $derived(lists.reduce((n, l) => n + l.count, 0))
  const on = $derived(lists.filter((l) => l.enabled).length)
  const nice = (n) => n.toLocaleString('en')
  const baseName = (l) => l.name.split('/').slice(1).join('/') || l.name
  const folderState = (fl) => (fl.every((l) => l.enabled) ? 'true' : fl.some((l) => l.enabled) ? 'mixed' : 'false')
  const sameRole = (fl) => (fl.every((l) => l.role === fl[0].role) ? fl[0].role : '')
  const names = (o) => new Intl.ListFormat('en').format(Object.keys(o))
  // What a list feeds, as a short plain line (the counts are in the tooltip).
  const feeds = (l) => (Object.keys(l.serves.slots).length ? `Feeds ${names(l.serves.slots)}` : 'Fits no part of a picture: offered only when a request is about it')
  const how = (l) => (l.serves.by === 'you' ? 'set by you' : l.serves.by === 'meaning' ? 'read by meaning' : 'guessed from the name and words')
  const counts = (l) => Object.entries(l.serves.slots).map(([k, n]) => `${k}: ${n} entries`).join('\n')
  const longNote = (l) => `${nice(l.serves.long)} ${l.serves.long === 1 ? 'entry is' : 'entries are'} over ${data.max_entry} characters, so ${l.serves.long === 1 ? 'it is' : 'they are'} not offered as ideas. Typing __${baseName(l)}__ still uses ${l.serves.long === 1 ? 'it' : 'them'}.`
  function importNote(res) {
    const d = res.imported.serves
    if (!d) return ''
    const parts = Object.entries(d.slots).map(([k, n]) => `${nice(n)} feed ${k}`)
    if (d.mixed) parts.push(`${nice(d.mixed)} mixed`)
    if (d.none) parts.push(`${nice(d.none)} match only`)
    return `: ${parts.join(', ')}${d.low ? ' (read by name for now, entries are read by meaning in the background)' : ''}`
  }

  async function load() {
    try {
      data = await api.wildcards()
      if (data.sources.length === 1 && path === DEFAULT_PATH) path = data.sources[0]
    } catch (e) {
      data = { lists: [], roles: [], sources: [] }
      failed = e.message
    }
  }
  onMount(load)
  // a list was deleted (here, or in another window): read the lists again
  let seen = untrack(() => live.wildcardsStamp)
  $effect(() => {
    if (live.wildcardsStamp !== seen) {
      seen = live.wildcardsStamp
      untrack(load)
    }
  })
  const deleted = () => {
    if (openList === deleting) openList = null
    deleting = null
  }

  async function doImport(from) {
    busy = true
    failed = ''
    try {
      const res = await api.importWildcards(from)
      data = res
      importing = false
      path = from
      toast(`Imported ${nice(res.imported.lists)} lists (${nice(res.imported.entries)} entries)${importNote(res)}`)
    } catch (e) {
      failed = e.message
    } finally {
      busy = false
    }
  }
  async function reimport() {
    busy = true
    try {
      let last
      for (const source of data.sources) last = await api.importWildcards(source)
      data = last
      toast('Imported again. Your on/off and role settings are kept.')
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
  async function patchList(l, change) {
    try {
      Object.assign(l, await api.patchWildcard(l.name, change))
    } catch (e) {
      toastError(e)
    }
  }
  async function patchFolder(f, change) {
    try {
      data = await api.patchWildcardFolder(f.folder, change)
    } catch (e) {
      toastError(e)
    }
  }
  async function toggleList(l) {
    openList = openList === l.name ? null : l.name
    if (openList && !entries[l.name]) {
      try {
        entries[l.name] = (await api.wildcard(l.name)).entries
      } catch (e) {
        toastError(e)
      }
    }
  }
  const intro = 'Lists of ideas, one per line, copied from your Forge wildcard folder. When the planner writes prompts, the app draws a few random lines from lists that suit your request and offers them to the LLM, which picks the ones that fit. Every image gets different ingredients. You can also type __list-name__ or {a|b|c} into a request or a prompt: the app picks a random line for each image.'
  const roleHelp = 'Setting a role tells the app what the whole list is for. Roles for a setting, camera shot, palette, outfit, pose or gaze are offered whenever your request leaves that open. Other roles (style, artist, material…) are only offered when your request mentions the list’s topic. Automatic lets the app read the entries: one list can then feed several parts of a picture.'
</script>

<section class="stack lg">
  <div class="section-head">
    <h2>Wildcards</h2>
    <InfoTip title="Wildcards" text={intro} />
    {#if lists.length}<span class="n">{nice(lists.length)} lists, {nice(total)} entries, {on} on</span>{/if}
    <span class="spacer"></span>
    {#if lists.length}
      {#if data.sources.length}
        <button class="btn" onclick={reimport} disabled={busy} title="Copy the folder again to pick up changes. Your on/off and role settings are kept.">
          <span class:spin={busy} style="display:grid"><Icon name="retry" /></span>Re-import
        </button>
      {/if}
      <button class="btn" onclick={() => (importing = !importing)} aria-expanded={importing}><Icon name={importing ? 'x' : 'import'} />{importing ? 'Close' : 'Import folder'}</button>
    {/if}
  </div>

  {#if data === null}
    <div class="skeleton" aria-hidden="true"></div>
  {:else}
    <div class="reveal" class:open={importing && lists.length}>
      <div>
        <div class="composer" style="gap:8px">
          <h3>Import a wildcard folder</h3>
          {@render form()}
        </div>
      </div>
    </div>

    {#if !lists.length}
      <div class="empty fade-in">
        <div class="glyph"><Icon name="book" /></div>
        <h3>No wildcards yet</h3>
        <p>Import your Forge “Dynamic Prompts” wildcard folder. The app copies the lists and never changes your originals.</p>
        <div class="wc-empty-form">{@render form()}</div>
      </div>
    {:else}
      <div class="wc-folders">
        {#each folders as f (f.key)}
          {@const st = folderState(f.lists)}
          {@const role = sameRole(f.lists)}
          <div class="wc-folder" class:open={openFolders[f.key]}>
            <div class="wc-row wc-folder-row">
              <button class="wc-name" onclick={() => (openFolders[f.key] = !openFolders[f.key])} aria-expanded={!!openFolders[f.key]}>
                <span class="wc-chev" class:turned={openFolders[f.key]}><Icon name="chevron" /></span>
                <span class="wc-folder-name">{f.key}</span>
                <span class="wc-count">{f.lists.length} {f.lists.length === 1 ? 'list' : 'lists'}</span>
              </button>
              {#if f.folder}
                <select class="select wc-role" aria-label="Role of every list in {f.key}" value={role} onchange={(e) => e.currentTarget.value && patchFolder(f, { role: e.currentTarget.value })}>
                  {#if !role}<option value="">mixed roles</option>{/if}
                  {#each data.roles as r}<option value={r}>{r}</option>{/each}
                </select>
              {:else}<span></span>{/if}
              <button class="switch" role="switch" aria-checked={st} aria-label="Use every list in {f.key}" title={st === 'true' ? 'Switch the whole folder off' : 'Switch the whole folder on'}
                onclick={() => (f.folder ? patchFolder(f, { enabled: st !== 'true' }) : f.lists.forEach((l) => patchList(l, { enabled: st !== 'true' })))}></button>
              <span></span>
            </div>
            {#if openFolders[f.key]}
              <div class="wc-lists">
                {#each f.lists as l (l.name)}
                  <div class="wc-row wc-list" class:off={!l.enabled}>
                    <div class="wc-main">
                      <button class="wc-name" onclick={() => toggleList(l)} aria-expanded={openList === l.name} title={l.name}>
                        <span class="wc-chev" class:turned={openList === l.name}><Icon name="chevron" /></span>
                        <span class="wc-list-name">{baseName(l)}</span>
                        <span class="wc-count">{nice(l.count)}</span>
                      </button>
                      <p class="wc-serves" title={counts(l)}>
                        <span>{feeds(l)}</span><span class="wc-how">{how(l)}</span>{#if !l.serves.sure}<span class="wc-low">low confidence</span>{/if}
                        {#if l.serves.long}<span class="wc-long" title={longNote(l)}>{nice(l.serves.long)} too long to offer</span>{/if}
                      </p>
                    </div>
                    <select class="select wc-role" aria-label="Role of {l.name}: set it to correct what the app read" value={l.role_set ? l.role : ''}
                      onchange={(e) => patchList(l, { role: e.currentTarget.value })}>
                      <option value="">Automatic</option>
                      {#each data.roles as r}<option value={r}>{r}</option>{/each}
                    </select>
                    <button class="switch" role="switch" aria-checked={l.enabled} aria-label="Use {l.name}" onclick={() => patchList(l, { enabled: !l.enabled })}></button>
                    <button class="icon-btn sm danger" aria-label="Delete {l.name}" title="Delete this list and its file" onclick={() => (deleting = l.name)}><Icon name="trash" /></button>
                  </div>
                  {#if openList === l.name}
                    <div class="wc-preview">
                      {#if entries[l.name]}
                        <p class="wc-entries mono">{#each entries[l.name] as e}<span>{e}</span>{/each}</p>
                      {:else}<p class="muted">Loading…</p>{/if}
                    </div>
                  {/if}
                {/each}
              </div>
            {/if}
          </div>
        {/each}
      </div>
      <p class="wc-foot muted">Each list is read from its entries, whatever its name or folder. A role <InfoTip text={roleHelp} label="About roles" /> corrects that reading and always wins; Automatic goes back to it. Switch a list off to keep it out of the planner’s ideas; typing its name still works.</p>
    {/if}
  {/if}
</section>

{#if deleting}
  <DeleteDialog kind="wildcard" target={deleting} name={deleting} ondone={deleted} onclose={() => (deleting = null)} />
{/if}

{#snippet form()}
  <div class="wc-form">
    <input class="field mono" bind:value={path} aria-label="Wildcard folder" spellcheck="false" placeholder="Folder with your wildcard .txt files" onkeydown={(e) => e.key === 'Enter' && path.trim() && !busy && doImport(path)} />
    <button class="btn primary" onclick={() => doImport(path)} disabled={busy || !path.trim()}>
      {#if busy}<span class="spin" style="display:grid"><Icon name="loader" /></span>Importing{:else}<Icon name="import" />Import{/if}
    </button>
  </div>
  {#if failed}<p class="wc-err" role="alert">{failed}</p>{/if}
{/snippet}

<style>
  .wc-folders { display: grid; border: 1px solid var(--line); border-radius: var(--r-2xl); background: var(--surface); overflow: hidden; }
  .wc-folder + .wc-folder { border-top: 1px solid var(--line); }
  .wc-row { display: grid; grid-template-columns: minmax(0, 1fr) 150px 34px 28px; gap: 14px; align-items: center; padding: 6px 16px; min-height: 44px; }
  .wc-main { display: grid; min-width: 0; }
  .wc-serves { display: flex; flex-wrap: wrap; gap: 2px 10px; margin: -4px 0 4px 24px; color: var(--muted); font-size: 12.5px; line-height: 1.4; }
  .wc-how { color: var(--faint); }
  .wc-low { color: var(--amber); }
  .wc-long { color: var(--faint); cursor: help; }
  .wc-name { display: flex; align-items: center; gap: 8px; min-width: 0; text-align: left; height: 32px; border-radius: var(--r-md); }
  .wc-name:focus-visible { outline: 2px solid var(--mint); outline-offset: 2px; }
  .wc-chev { display: grid; color: var(--muted); transform: rotate(-90deg); transition: transform 0.3s var(--ease); flex: none; }
  .wc-chev.turned { transform: none; }
  .wc-folder-name { font-weight: 500; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wc-count { color: var(--muted); font-size: 12.5px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .wc-role { height: 28px; font-size: 13px; }
  .wc-lists { border-top: 1px solid var(--line); background: var(--bg); }
  .wc-list { padding-left: 38px; }
  .wc-list + .wc-list, .wc-preview + .wc-list { border-top: 1px solid var(--line); }
  .wc-list-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-soft); }
  .wc-list.off .wc-list-name, .wc-list.off .wc-count { opacity: 0.5; }
  .switch[aria-checked='mixed'] { background: var(--raised); border-color: var(--mint-line); }
  .switch[aria-checked='mixed']::after { transform: translateX(7px); background: var(--mint); opacity: 0.7; }
  .wc-preview { padding: 2px 16px 12px 38px; }
  .wc-entries { display: flex; flex-wrap: wrap; gap: 6px; max-height: 190px; overflow-y: auto; overscroll-behavior: contain; margin: 0; padding: 10px; border: 1px solid var(--line); border-radius: var(--r-md); background: var(--surface); font-size: 12px; scrollbar-width: thin; }
  .wc-entries span { padding: 2px 7px; border-radius: var(--r-sm); background: var(--raised); color: var(--text-soft); max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wc-form { display: flex; gap: 10px; width: 100%; }
  .wc-form .field { flex: 1; }
  .wc-empty-form { width: min(560px, 100%); display: grid; gap: 8px; }
  .wc-err { color: var(--red); font-size: 13px; text-align: left; }
  .wc-foot { font-size: 12.5px; max-width: 70ch; }
  @media (max-width: 700px) { .wc-row { grid-template-columns: minmax(0, 1fr) 110px 34px 28px; gap: 8px; padding: 6px 10px; } .wc-list { padding-left: 22px; } }
</style>
