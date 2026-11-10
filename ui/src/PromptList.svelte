<script>
  // The prompts of a job, one row per image. Click a prompt to edit it (saving locks it); the padlock locks a single prompt,
  // and a re-plan then keeps it word for word. Under each prompt: the one-line reason from the prompt writer.
  import Icon from './Icon.svelte'
  import { autogrow } from './motion.js'

  // onchange(prompts, locks): you edited a prompt. onlock(index, locked): you toggled one padlock.
  let { prompts, locks, items = [], onchange, onlock } = $props()

  let editing = $state(-1)
  let draft = $state('')
  const flag = (i) => !!locks[i]

  const HOW = { llm: 'chosen by the LLM', phrase: 'your request mentions it', meaning: 'close in meaning to your request', keyword: 'matches your request', slot: 'an open part of the picture' }
  // The tooltip of "From your wildcards": which lists the ideas came from and why.
  const listsNote = (ideas) => ['Ideas drawn from your wildcard lists that this prompt uses.',
    ...(ideas.lists ?? []).map((l) => `${l.list}: ${HOW[l.by] ?? l.by}${l.by === 'llm' && l.why ? ` (${l.why})` : ''}`)].join('\n')

  // The tag check's changes for one prompt, as one calm line: "`1old man` → `old man`, removed `solo`".
  const tagLine = (recs) => recs.map((r) => (r.from && r.to ? `\`${r.from}\` → \`${r.to}\`` : r.to ? `added \`${r.to}\`` : r.from ? `removed \`${r.from}\`` : r.why)).join(', ')
  const tagWhy = (recs) => recs.map((r) => r.why).filter(Boolean).join('\n')

  function startEdit(i) {
    editing = i
    draft = prompts[i]
  }
  function commit() {
    if (editing < 0) return
    const i = editing
    editing = -1
    if (draft === prompts[i] && locks[i]) return
    onchange(prompts.map((p, j) => (j === i ? draft : p)), prompts.map((_, j) => (j === i ? true : flag(j))))
  }
  const focusEnd = (node) => {
    node.focus()
    node.setSelectionRange(node.value.length, node.value.length)
  }
  function key(e) {
    if (e.key === 'Escape') {
      e.preventDefault()
      editing = -1
    } else if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      commit()
    }
  }
</script>

<div class="prompts">
  {#each prompts as text, i}
    <div class="p" class:locked={flag(i)} class:editing={editing === i}>
      <span class="n">{i + 1}</span>
      <div class="pb">
        {#if editing === i}
          <textarea rows="1" aria-label="Prompt {i + 1}" bind:value={draft} use:autogrow use:focusEnd onblur={commit} onkeydown={key}></textarea>
        {:else}
          <button class="t" class:empty={!text.trim()} onclick={() => startEdit(i)} aria-label="Edit prompt {i + 1}">{text.trim() ? text : 'Write a prompt'}</button>
        {/if}
        {#if items[i]?.reason}<span class="r">{items[i].reason}</span>{/if}
        {#if items[i]?.tagcheck?.length}<span class="tagcheck-line" title={tagWhy(items[i].tagcheck)}><Icon name="check" />Tag check: {tagLine(items[i].tagcheck)}</span>{/if}
        {#if items[i]?.ideas?.used?.length}<span class="wc-from" title={listsNote(items[i].ideas)}><Icon name="book" />From your wildcards: {items[i].ideas.used.join(', ')}</span>{/if}
      </div>
      <button class="lockbtn" onmousedown={(e) => e.preventDefault()} onclick={() => onlock(i, !flag(i))} aria-pressed={flag(i)}
        title={flag(i) ? 'Locked: a re-plan keeps this prompt as it is. Click to unlock.' : 'Lock this prompt so a re-plan keeps it as it is.'}
        aria-label="{flag(i) ? 'Unlock' : 'Lock'} prompt {i + 1}"><Icon name={flag(i) ? 'lock' : 'unlock'} /></button>
    </div>
  {/each}
</div>

<style>
  .wc-from { display: flex; align-items: flex-start; gap: 6px; color: var(--muted); font-size: 12.5px; line-height: 1.4; }
  .tagcheck-line { display: flex; align-items: flex-start; gap: 6px; color: var(--muted); font-size: 12.5px; line-height: 1.4; overflow-wrap: anywhere; }
  .tagcheck-line :global(svg.i) { width: 12px; height: 12px; margin-top: 2.5px; flex: none; }
  .wc-from :global(svg.i) { width: 12px; height: 12px; margin-top: 2.5px; flex: none; }
</style>
