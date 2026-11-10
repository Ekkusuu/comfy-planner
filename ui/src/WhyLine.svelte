<script>
  // One quiet line under a field: who set it (sparkle = the LLM or the workflow, lock = you), why, and any clamp note or issue.
  import Icon from './Icon.svelte'

  let { field, fallback = '', issues = [] } = $props()

  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s)
  const locked = $derived(field?.source === 'locked')
  const text = $derived.by(() => {
    const r = (field?.reason ?? '').trim()
    if (locked) return !r || r === 'set by hand' ? 'Locked by you' : cap(r)
    return cap(r) || fallback
  })
</script>

{#if field}
  <span class="why" class:locked><Icon name={locked ? 'lock' : 'spark'} /><span>{text}</span></span>
  {#if field.note}<span class="why flag"><Icon name="alert" /><span>{cap(field.note)}</span></span>{/if}
{/if}
{#each issues as issue}
  <span class="why flag" class:block={issue.severity === 'blocking'}><Icon name="alert" /><span>{issue.message}</span></span>
{/each}
