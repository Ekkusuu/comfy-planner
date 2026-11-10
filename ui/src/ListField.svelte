<script>
  // A list edited as text: one item per line (lines) or comma separated (commas).
  // The text is kept locally while you type, so blank lines and trailing commas aren't removed under the cursor.
  let { value = $bindable([]), mode = 'lines', rows = 3, placeholder = '', mono = false, format = (x) => x, parse = (x) => x, id } = $props()

  const sep = $derived(mode === 'lines' ? '\n' : ', ')
  const split = (t) => t.split(mode === 'lines' ? '\n' : ',').map((x) => x.trim()).filter(Boolean).map(parse).filter((x) => x != null)
  const join = (v) => v.map(format).join(sep)

  let text = $state(join(value))
  // pick up changes made elsewhere (a reload, an accepted re-generation), but not our own typing
  $effect(() => {
    const incoming = JSON.stringify(value)
    if (incoming !== JSON.stringify(split(text))) text = join(value)
  })
  function input(e) {
    text = e.currentTarget.value
    value = split(text)
  }
</script>

{#if mode === 'lines'}
  <textarea class="textarea" class:mono {id} {rows} {placeholder} value={text} oninput={input} spellcheck="false"></textarea>
{:else}
  <input class="field" class:mono {id} {placeholder} value={text} oninput={input} spellcheck="false" />
{/if}
