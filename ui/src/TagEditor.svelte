<script>
  // The user's own tags on a LoRA: the tags it has (click × to remove), a box to add one (Enter adds, commas separate),
  // and tags already used on other LoRAs as suggestions. `onadd(list)` / `onremove(tag)` may return a promise.
  let { tags, all = [], onadd, onremove, label = 'Tags', id, busy = false, off = false } = $props() // off: nothing to tag yet

  let text = $state('')
  const norm = (t) => t.replace(/\s+/g, ' ').trim().toLowerCase()
  const suggestions = $derived.by(() => {
    const typed = norm(text)
    return all.filter((t) => !tags.includes(t) && (!typed || t.includes(typed))).slice(0, 8)
  })

  async function add(raw) {
    const list = raw.split(',').map(norm).filter(Boolean)
    if (!list.length) return
    text = ''
    await onadd(list)
  }
</script>

<div class="te">
  {#if tags.length}
    <ul class="te-list" aria-label={label}>
      {#each tags as t (t)}
        <li class="te-tag"><span>{t}</span><button type="button" class="te-x" aria-label="Remove tag {t}" disabled={busy || off} onclick={() => onremove(t)}>×</button></li>
      {/each}
    </ul>
  {/if}
  <form class="te-form" onsubmit={(e) => { e.preventDefault(); add(text) }}>
    <input class="field te-input" {id} aria-label="Add a tag" placeholder="Add a tag, press Enter" bind:value={text} maxlength="80" autocomplete="off" spellcheck="false" disabled={off} />
  </form>
  {#if suggestions.length}
    <ul class="te-sugg" aria-label="Tags already in use">
      {#each suggestions as t (t)}
        <li><button type="button" class="te-add" disabled={busy || off} onclick={() => add(t)}>+ {t}</button></li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .te { display: grid; gap: 8px; min-width: 0; }
  .te-list, .te-sugg { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 6px; }
  .te-tag { display: inline-flex; align-items: center; max-width: 100%; height: 24px; padding: 0 2px 0 9px; border-radius: var(--r-sm); background: var(--mint-dim); border: 1px solid var(--mint-line); color: var(--text); font-size: 12.5px; }
  .te-tag span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .te-x { width: 20px; height: 20px; border-radius: 4px; color: var(--muted); font-size: 15px; line-height: 1; display: grid; place-items: center; }
  .te-x:hover:not(:disabled) { color: var(--text); background: rgba(255, 255, 255, 0.08); }
  .te-input { height: 30px; font-size: 13px; }
  .te-add { height: 22px; padding: 0 8px; border-radius: var(--r-sm); border: 1px dashed var(--line2); color: var(--muted); font-size: 12px; max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .te-add:hover:not(:disabled) { color: var(--text); border-color: var(--mint-line); }
</style>
