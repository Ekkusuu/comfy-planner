<script>
  // A list of short text items shown as chips. Enter or a comma adds, Backspace on an empty box removes the last.
  let { value = $bindable([]), placeholder = 'Add a tag', label = '', invalid = false } = $props()
  let text = $state('')
  let box

  function add(raw) {
    const items = raw.split(',').map((x) => x.trim()).filter((x) => x && !value.includes(x))
    if (items.length) value = [...value, ...items]
    text = ''
  }
  function key(e) {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault()
      add(text)
    } else if (e.key === 'Backspace' && !text && value.length) value = value.slice(0, -1)
  }
  const remove = (tag) => (value = value.filter((x) => x !== tag))
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="tags" class:invalid onclick={() => box.focus()}>
  {#each value as tag (tag)}
    <span class="tag">{tag}<button type="button" aria-label="Remove {tag}" onclick={(e) => { e.stopPropagation(); remove(tag) }}>×</button></span>
  {/each}
  <input bind:this={box} bind:value={text} {placeholder} aria-label={label} spellcheck="false" onkeydown={key} onblur={() => add(text)} onpaste={(e) => { const t = e.clipboardData.getData('text'); if (t.includes(',')) { e.preventDefault(); add(t) } }} />
</div>

<style>
  .tags { display: flex; flex-wrap: wrap; gap: 6px; padding: 5px; min-height: 36px; background: var(--bg); border: 1px solid var(--line2); border-radius: var(--r-md); cursor: text; transition: border-color 0.2s var(--ease), box-shadow 0.2s var(--ease); }
  .tags:focus-within { border-color: var(--mint-line); box-shadow: 0 0 0 3px var(--mint-dim); }
  .tags.invalid { border-color: var(--red-line); }
  .tag { height: 24px; padding: 0 4px 0 9px; border-radius: var(--r-sm); background: var(--raised); display: inline-flex; align-items: center; gap: 2px; font-size: 12.5px; color: var(--text-soft); }
  .tag button { width: 18px; height: 18px; border-radius: 4px; color: var(--faint); font-size: 14px; line-height: 1; display: grid; place-items: center; transition: color 0.2s var(--ease), background 0.2s var(--ease); }
  .tag button:hover { color: var(--text); background: var(--float); }
  input { flex: 1; min-width: 110px; height: 24px; background: none; border: 0; outline: none; color: var(--text); font: inherit; font-size: 13px; padding: 0 4px; }
  input::placeholder { color: var(--faint); }
</style>
