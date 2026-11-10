<script module>
  let count = 0
</script>

<script>
  // A small "i" that explains the thing next to it. Opens on hover, on keyboard focus and on tap; Esc closes it.
  // The bubble is fixed-positioned so cards and disclosures that clip their content can't cut it off.
  import Icon from './Icon.svelte'

  let { text, title = '', label = 'What is this?' } = $props()

  const id = `infotip-${++count}`
  let open = $state(false)
  let pinned = false
  let at = $state({ x: 0, y: 0, below: false })
  let btn
  let timer

  function place() {
    const r = btn.getBoundingClientRect()
    const below = r.top < 150
    at = { x: Math.min(Math.max(r.left + r.width / 2, 138), innerWidth - 138), y: below ? r.bottom + 8 : innerHeight - r.top + 8, below }
  }
  function show(delay = 0) {
    clearTimeout(timer)
    timer = setTimeout(() => { place(); open = true }, delay)
  }
  function hide() {
    clearTimeout(timer)
    pinned = false
    open = false
  }
  const enter = (e) => e.pointerType === 'mouse' && show(140)
  const leave = (e) => e.pointerType === 'mouse' && !pinned && hide()
  function click() {
    if (open && pinned) return hide()
    pinned = true
    show()
  }
  function key(e) {
    if (e.key === 'Escape' && open) {
      e.stopPropagation()
      hide()
    }
  }
  $effect(() => {
    if (!open) return
    const away = (e) => { if (!btn.contains(e.target)) hide() }
    addEventListener('scroll', hide, true)
    addEventListener('pointerdown', away, true)
    return () => {
      removeEventListener('scroll', hide, true)
      removeEventListener('pointerdown', away, true)
    }
  })
</script>

<span class="wrap">
  <button type="button" bind:this={btn} class="info" class:on={open} aria-label={label} aria-describedby={id}
    onpointerenter={enter} onpointerleave={leave} onfocus={() => show()} onblur={hide} onclick={click} onkeydown={key}>
    <Icon name="info" />
  </button>
  <span {id} role="tooltip" class="bubble" class:open class:below={at.below}
    style="left:{at.x}px;{at.below ? `top:${at.y}px` : `bottom:${at.y}px`}">
    {#if title}<b>{title}</b>{/if}{text}
  </span>
</span>

<style>
  .wrap { display: inline-flex; vertical-align: middle; flex: none; }
  .info { width: 20px; height: 20px; margin: -3px 0; border-radius: 50%; display: inline-grid; place-items: center; color: var(--faint); transition: color 0.2s var(--ease); }
  .info:hover, .info.on { color: var(--text-soft); }
  .info :global(svg.i) { width: 13px; height: 13px; }
  .bubble {
    position: fixed; z-index: 60; width: max-content; max-width: 260px; padding: 8px 11px; border-radius: var(--r-md);
    background: var(--float); border: 1px solid var(--line2); box-shadow: var(--shadow-pop);
    color: var(--text); font-size: 12.5px; line-height: 1.45; font-weight: 400; text-align: left; white-space: normal;
    transform: translate(-50%, 4px); transform-origin: 50% 100%; opacity: 0; visibility: hidden; pointer-events: none;
    transition: opacity 0.18s var(--ease), transform 0.25s var(--ease), visibility 0s 0.25s;
  }
  .bubble.below { transform: translate(-50%, -4px); transform-origin: 50% 0; }
  .bubble.open { opacity: 1; visibility: visible; transform: translate(-50%, 0); transition-delay: 0s; }
  .bubble b { display: block; color: var(--muted); font-weight: 500; font-size: 11.5px; margin-bottom: 1px; }
</style>
