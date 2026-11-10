<script>
  // A small toolbar button that opens a list to choose from. Used for the LoRA filters (several values may be chosen,
  // each with a check mark and a count) and for the sort (one value; picking it closes the list).
  // Arrows, Home and End move through the rows; Enter and Space choose; Esc closes only this list and puts focus back
  // on the button; Tab leaves and closes it.
  import { tick } from 'svelte'
  import Icon from './Icon.svelte'
  import { pop, dismissable } from './motion.js'

  let {
    label, // the button's name: Kind, Family, Tags, Sort
    options, // [{ value, label?, count? }]
    selected, // the chosen values (several), or [the chosen value] when `single`
    single = false,
    onpick, // (value) => void
    tall = false, // a long list: scrolls inside a limited height
  } = $props()

  let open = $state(false)
  let right = $state(false) // open to the left of the button's right edge when there's no room on the right
  let active = $state(0) // the row that can be reached with Tab (roving)
  let wrap = $state(null)
  let button = $state(null)
  let rows = $state(null)

  const has = (v) => selected.includes(v)
  const textOf = (o) => o.label ?? o.value
  const current = $derived(single ? options.find((o) => has(o.value)) : null)

  async function show() {
    const r = button.getBoundingClientRect()
    const width = Math.min(single ? 184 : 248, innerWidth - 24)
    right = r.left + width > innerWidth - 12 && r.right >= width
    active = Math.max(0, options.findIndex((o) => has(o.value)))
    open = true
    await tick()
    rows?.querySelectorAll('button')[active]?.focus()
  }
  function close(refocus = true) {
    if (!open) return
    open = false
    if (refocus) button?.focus()
  }
  // Esc and a press outside close the list. A press on the button itself is the button's own toggle.
  function away(e) {
    if (e.type === 'pointerdown' && wrap?.contains(e.target)) return
    close(e.type === 'keydown')
  }
  function pick(value) {
    onpick(value)
    if (single) close()
  }
  function move(e) {
    const list = [...rows.querySelectorAll('button')]
    const at = list.indexOf(document.activeElement)
    const to = { ArrowDown: Math.min(at + 1, list.length - 1), ArrowUp: Math.max(at - 1, 0), Home: 0, End: list.length - 1 }[e.key]
    if (to === undefined) return
    e.preventDefault()
    active = to
    list[to]?.focus()
  }
  function leaving(e) {
    // focus went somewhere outside the list (Tab): close it, leaving focus where it went
    if (e.relatedTarget && !wrap.contains(e.relatedTarget)) close(false)
  }
</script>

<div class="fm" bind:this={wrap} onfocusout={leaving}>
  <button
    type="button"
    class="btn fm-btn"
    class:on={selected.length > 0 && !single}
    bind:this={button}
    aria-haspopup="true"
    aria-expanded={open}
    aria-label={single ? `Sort by: ${current ? textOf(current) : ''}` : selected.length ? `${label}, ${selected.length} chosen` : label}
    onclick={() => (open ? close(false) : show())}
    onkeydown={(e) => open && e.key === 'Escape' && (e.stopPropagation(), close())}
  >
    {#if single}<span class="fm-k">{label}:</span><span class="fm-v">{current ? textOf(current) : ''}</span>
    {:else}<span>{label}</span>{#if selected.length}<span class="fm-n">{selected.length}</span>{/if}{/if}
    <Icon name="chevron" />
  </button>

  {#if open}
    <div class="fm-pop" class:right class:single class:tall transition:pop={{ y: -4, duration: 200 }} use:dismissable={away}>
      <div class="fm-rows" bind:this={rows} role={single ? 'radiogroup' : 'group'} aria-label={single ? 'Sort by' : `Filter by ${label.toLowerCase()}`} onkeydown={move}>
        {#each options as o, i (o.value)}
          {@const on = has(o.value)}
          <button
            type="button"
            class="fm-row"
            class:on
            role={single ? 'radio' : undefined}
            aria-checked={single ? on : undefined}
            aria-pressed={single ? undefined : on}
            tabindex={i === active ? 0 : -1}
            onfocus={() => (active = i)}
            onclick={() => pick(o.value)}
          >
            <span class="fm-check" aria-hidden="true"><Icon name="check" /></span>
            <span class="fm-text">{textOf(o)}</span>
            {#if o.count !== undefined}<span class="fm-count">{o.count}</span>{/if}
          </button>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .fm { position: relative; display: inline-flex; }
  .fm-btn { color: var(--text-soft); padding-right: 9px; }
  .fm-btn :global(svg.i) { color: var(--muted); transition: transform 0.25s var(--ease); }
  .fm-btn[aria-expanded='true'] :global(svg.i) { transform: rotate(180deg); }
  .fm-btn[aria-expanded='true'], .fm-btn.on { border-color: rgba(255, 255, 255, 0.3); color: var(--text); }
  .fm-k { color: var(--muted); }
  .fm-v { color: var(--text); }
  .fm-n { min-width: 18px; height: 18px; padding: 0 5px; border-radius: 9px; background: var(--raised); color: var(--text); font-size: 11.5px; line-height: 18px; text-align: center; font-variant-numeric: tabular-nums; }

  .fm-pop { position: absolute; z-index: 40; left: 0; top: calc(100% + 6px); width: 248px; max-width: calc(100vw - 24px); padding: 6px; border-radius: var(--r-xl); background: var(--float); border: 1px solid var(--line2); box-shadow: var(--shadow-pop); transform-origin: 16px 0; }
  .fm-pop.single { width: 184px; }
  .fm-pop.right { left: auto; right: 0; transform-origin: calc(100% - 16px) 0; }
  .fm-pop.tall .fm-rows { max-height: 284px; overflow-y: auto; overscroll-behavior: contain; }
  .fm-rows { display: grid; grid-template-columns: minmax(0, 1fr); gap: 1px; }
  .fm-row { display: flex; align-items: center; gap: 8px; width: 100%; min-height: 30px; padding: 0 8px; border-radius: var(--r-md); text-align: left; color: var(--text-soft); font-size: 13px; transition: background 0.15s var(--ease), color 0.15s var(--ease); }
  .fm-row:hover { background: rgba(255, 255, 255, 0.06); color: var(--text); }
  .fm-row.on { color: var(--text); }
  .fm-row:focus-visible { outline: 2px solid var(--mint); outline-offset: -2px; }
  .fm-check { width: 14px; height: 14px; flex: none; display: grid; place-items: center; color: transparent; }
  .fm-check :global(svg.i) { width: 14px; height: 14px; stroke-width: 2.2; }
  .fm-row.on .fm-check { color: var(--text); }
  .fm-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fm-count { flex: none; color: var(--muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
</style>
