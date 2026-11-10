<script>
  import { tick } from 'svelte'
  import Icon from './Icon.svelte'
  import { api } from './api.js'
  import { wheelScroll } from './wheelScroll.js'

  // A radio group of cover tiles in one horizontal, snapping row (scales to many workflows without growing taller).
  let { workflows, value, onchange } = $props()

  let scroller = $state(null)
  let broken = $state({}) // image id -> its cover failed to load
  let edge = $state({ start: false, end: false })

  // the variant ("T2I + hires", "style LoRA") is shown only when two workflows share a name; the hover title has it all
  const names = $derived(workflows.reduce((m, w) => m.set(parts(w.title).name, (m.get(parts(w.title).name) ?? 0) + 1), new Map()))

  /** Readable two-part name: "Name - variant (note)" becomes name + variant; underscores become spaces. Display only. */
  function parts(title) {
    let t = title.replace(/(\d)_(\d)/g, '$1.$2').replace(/_/g, ' ').replace(/\s+/g, ' ').trim()
    if (t === t.toLowerCase()) t = t.charAt(0).toUpperCase() + t.slice(1) // file-name style titles
    const dash = t.match(/^(.+?)\s+[-–—]\s+(.+)$/)
    if (dash) return { name: dash[1], variant: dash[2] }
    const paren = t.match(/^(.+?)\s+\((.+)\)$/)
    if (paren) return { name: paren[1], variant: paren[2] }
    return { name: t, variant: '' }
  }

  function measure() {
    if (!scroller) return
    const { scrollLeft, scrollWidth, clientWidth } = scroller
    edge = { start: scrollLeft > 4, end: scrollLeft + clientWidth < scrollWidth - 4 }
  }
  $effect(() => {
    workflows.length
    measure()
  })
  $effect(() => {
    if (!scroller) return
    const ro = new ResizeObserver(measure)
    ro.observe(scroller)
    return () => ro.disconnect()
  })

  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches
  function page(dir) {
    scroller.scrollBy({ left: dir * scroller.clientWidth * 0.8, behavior: reduced() ? 'auto' : 'smooth' })
  }
  /** Scroll only the row (scrollIntoView would also move the clipped panels around it). */
  function reveal(id) {
    const el = scroller?.querySelector(`[data-id="${CSS.escape(id)}"]`)
    if (!el) return
    const pad = 18
    const left = el.offsetLeft - pad
    const right = el.offsetLeft + el.offsetWidth + pad
    const to = left < scroller.scrollLeft ? left : right > scroller.scrollLeft + scroller.clientWidth ? right - scroller.clientWidth : null
    if (to !== null) scroller.scrollTo({ left: to, behavior: reduced() ? 'auto' : 'smooth' })
  }
  $effect(() => {
    if (value && scroller) tick().then(() => reveal(value))
  })

  async function key(e, i) {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key]
    const to = e.key === 'Home' ? 0 : e.key === 'End' ? workflows.length - 1 : step ? (i + step + workflows.length) % workflows.length : null
    if (to === null) return
    e.preventDefault()
    onchange(workflows[to].id)
    await tick()
    scroller.querySelector(`[data-id="${CSS.escape(workflows[to].id)}"]`)?.focus()
  }
</script>

<div class="picker">
  <div class="rowwrap" class:start={edge.start} class:end={edge.end}>
    <div class="row" role="radiogroup" aria-label="Workflow" bind:this={scroller} onscroll={measure} use:wheelScroll>
      {#each workflows as w, i (w.id)}
        {@const p = parts(w.title)}
        {@const label = p.variant && names.get(p.name) > 1 ? `${p.name} · ${p.variant}` : p.name}
        {@const on = value === w.id}
        <button
          class="tile"
          class:on
          role="radio"
          aria-checked={on}
          tabindex={on || (!value && i === 0) ? 0 : -1}
          data-id={w.id}
          aria-label={w.title}
          aria-description={w.purpose || undefined}
          onclick={() => onchange(w.id)}
          onkeydown={(e) => key(e, i)}
        >
          <span class="cover">
            {#if w.cover_image_id !== null && !broken[w.cover_image_id]}
              <img src={api.thumbUrl(w.cover_image_id, 512)} alt="" loading="lazy" decoding="async" draggable="false" onerror={() => (broken[w.cover_image_id] = true)} />
            {:else}
              <span class="ph"><Icon name={w.kind === 'edit' ? 'wand' : 'image'} /></span>
            {/if}
            {#if w.purpose}<span class="about" aria-hidden="true"><span class="about-text">{w.purpose}</span></span>{/if}
            {#if w.kind === 'edit'}<span class="kind">Image edit</span>{/if}
            <span class="tick"><Icon name="check" /></span>
          </span>
          <span class="meta">
            <span class="name">{label}</span>
            {#if w.best_for}<span class="use">{w.best_for}</span>{/if}
          </span>
        </button>
      {/each}
    </div>
  </div>
  {#if edge.start || edge.end}
    <div class="nav" aria-hidden="true">
      <button class="icon-btn" tabindex="-1" aria-label="Scroll left" disabled={!edge.start} onclick={() => page(-1)}><Icon name="chevleft" /></button>
      <button class="icon-btn" tabindex="-1" aria-label="Scroll right" disabled={!edge.end} onclick={() => page(1)}><Icon name="chevright" /></button>
    </div>
  {/if}
</div>

<style>
  .picker { display: grid; grid-template-columns: minmax(0, 1fr); gap: 8px; position: relative; min-width: 0; }
  .rowwrap { margin: 0 -18px; position: relative; }
  .rowwrap::before, .rowwrap::after { content: ''; position: absolute; top: 0; bottom: 0; width: 40px; z-index: 2; pointer-events: none; opacity: 0; transition: opacity 0.3s var(--ease); }
  .rowwrap::before { left: 0; background: linear-gradient(90deg, var(--surface), transparent); }
  .rowwrap::after { right: 0; background: linear-gradient(270deg, var(--surface), transparent); }
  .rowwrap.start::before, .rowwrap.end::after { opacity: 1; }
  .row { display: flex; gap: 12px; overflow-x: auto; padding: 4px 18px 8px; scroll-snap-type: x proximity; overscroll-behavior-x: contain; scroll-padding: 0 18px; scrollbar-width: none; }
  .row::-webkit-scrollbar { display: none; }

  .tile { flex: none; width: 228px; scroll-snap-align: start; text-align: left; display: grid; align-content: start; gap: 9px; padding: 0; border-radius: var(--r-xl); transition: transform 0.15s var(--ease); }
  .tile:active { transform: scale(0.98); }
  .tile:focus-visible { outline: none; }
  .cover { position: relative; display: block; aspect-ratio: 4 / 3; border-radius: var(--r-xl); overflow: hidden; background: var(--raised); box-shadow: 0 0 0 1px var(--line2); transition: box-shadow 0.25s var(--ease); }
  .tile:hover .cover { box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.24); }
  .tile.on .cover { box-shadow: 0 0 0 2px var(--mint); }
  .tile:focus-visible .cover { box-shadow: 0 0 0 2px var(--mint), 0 0 0 5px var(--mint-dim); }
  .cover img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; animation: fadein 0.45s var(--ease) backwards; }
  .ph { position: absolute; inset: 0; display: grid; place-items: center; color: var(--faint); background: radial-gradient(120% 90% at 50% 0%, rgba(255, 255, 255, 0.06), transparent 70%), var(--raised); }
  .ph :global(svg.i) { width: 24px; height: 24px; }
  .kind { position: absolute; left: 8px; bottom: 8px; height: 20px; padding: 0 7px; border-radius: var(--r-sm); background: rgba(10, 10, 11, 0.78); border: 1px solid var(--line2); color: var(--text-soft); font-size: 11px; font-weight: 500; display: inline-flex; align-items: center; }
  /* hovering (or keyboard focus) fades the workflow's description in over its cover */
  .about { position: absolute; inset: 0; display: flex; align-items: flex-end; padding: 10px 11px; background: linear-gradient(180deg, rgba(10, 10, 11, 0.35) 0%, rgba(10, 10, 11, 0.88) 55%, rgba(10, 10, 11, 0.94) 100%); opacity: 0; transition: opacity 0.25s var(--ease); pointer-events: none; }
  .about-text { font-size: 12.5px; line-height: 1.45; color: rgba(255, 255, 255, 0.92); display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 7; line-clamp: 7; overflow: hidden; text-wrap: pretty; transform: translateY(4px); transition: transform 0.3s var(--ease); }
  .tile:hover .about, .tile:focus-visible .about { opacity: 1; }
  .tile:hover .about-text, .tile:focus-visible .about-text { transform: none; }
  .tile:hover .kind, .tile:focus-visible .kind { opacity: 0; }
  .kind { transition: opacity 0.2s var(--ease); }
  .tick { position: absolute; top: 8px; right: 8px; width: 20px; height: 20px; border-radius: 50%; background: var(--mint); color: var(--mint-ink); display: grid; place-items: center; opacity: 0; transform: scale(0.6); transition: opacity 0.2s var(--ease), transform 0.3s var(--ease); }
  .tick :global(svg.i) { width: 12px; height: 12px; stroke-width: 2.6; }
  .tile.on .tick { opacity: 1; transform: none; }

  .meta { display: grid; gap: 3px; min-width: 0; padding: 0 2px; }
  .name { font-size: 13px; font-weight: 500; line-height: 1.35; color: var(--text-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: color 0.2s var(--ease); }
  .use { font-size: 12px; line-height: 1.45; color: var(--muted); display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-clamp: 2; overflow: hidden; text-wrap: pretty; }
  .tile.on .use, .tile:hover .use { color: var(--text-soft); }
  .tile.on .name, .tile:hover .name { color: var(--text); }

  .nav { position: absolute; top: -34px; right: -6px; display: flex; gap: 2px; }
  .nav .icon-btn { width: 28px; height: 28px; }
  .nav .icon-btn:disabled { opacity: 0.3; }
</style>
