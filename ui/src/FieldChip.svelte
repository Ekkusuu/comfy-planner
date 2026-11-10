<script>
  // One job setting as a chip. Click opens a small editor; saving locks the value (mint), the lock icon unlocks it.
  import Icon from './Icon.svelte'
  import LoraCover from './LoraCover.svelte'
  import { pop, dismissable } from './motion.js'

  let {
    type, // number | select | size | lora | lora-add | count
    label = '', // muted key shown before the value
    display = '', // value text
    value, // number | string | {width,height} | {name,weight}
    locked = false,
    lockable = true,
    tip = null, // [title, body]
    ghost = false,
    flag = false, // a clamp note or an issue sits on this field
    wide = false,
    options = [], // select / lora-add
    min = 0, max = 100, step = 1,
    sizes = [],
    onsave, // (value, lock) => void
    onunlock, // () => void
    onremove, // lora only
    entryOf = () => undefined, // lora, lora-add: file name -> the Library's entry for it (its cover)
  } = $props()

  let open = $state(false)
  let right = $state(false)
  let d = $state({}) // draft
  let lock = $state(true)
  let btn
  let wrap

  const SIZES = [[1024, 1024], [832, 1216], [896, 1152], [1216, 832], [960, 1536], [1536, 960]]

  function show() {
    if (open) return close()
    lock = lockable ? true : false
    if (type === 'size') d = { w: value?.width ?? '', h: value?.height ?? '' }
    else if (type === 'lora') d = { weight: value.weight }
    else if (type === 'lora-add') d = { name: options[0] ?? '', weight: 0.8 }
    else d = { v: value ?? '' }
    right = wrap.getBoundingClientRect().left + 280 > innerWidth - 12
    open = true
  }
  function close() {
    open = false
    btn?.focus()
  }
  function save(e) {
    e?.preventDefault()
    let v
    if (type === 'number' || type === 'count') {
      v = Number(d.v)
      if (!Number.isFinite(v) || d.v === '') return
      v = Math.min(Math.max(v, min), max)
      if (step >= 1) v = Math.round(v)
    } else if (type === 'select') {
      v = String(d.v).trim()
      if (!v) return
    } else if (type === 'size') {
      const w = Math.round(Number(d.w)), h = Math.round(Number(d.h))
      if (!(w >= 64 && h >= 64)) return
      v = { width: w, height: h }
    } else if (type === 'lora') {
      v = { name: value.name, weight: Number(d.weight) }
      if (!Number.isFinite(v.weight)) return
    } else if (type === 'lora-add') {
      if (!String(d.name).trim()) return
      v = { name: String(d.name).trim(), weight: Number(d.weight) || 0.8 }
    }
    open = false
    onsave(v, lock)
  }
  const focusFirst = (node) => {
    node.querySelector('input:not([type=checkbox]), select')?.focus()
    node.querySelector('input')?.select?.()
  }
  const sizeActive = (w, h) => Number(d.w) === w && Number(d.h) === h
</script>

<span class="chip-wrap" class:locked={locked && lockable} class:open bind:this={wrap} use:dismissable={() => open && close()}>
  <button class="chip" class:locked={locked && lockable} class:wide class:add={type === 'lora-add'} class:open class:ghost bind:this={btn} onclick={show} aria-haspopup="dialog" aria-expanded={open}>
    {#if type === 'lora-add'}<Icon name="plus" />{/if}
    {#if label}<span class="k">{label}</span>{/if}
    {#if type === 'lora' && entryOf(value.name)}<span class="lcm"><LoraCover entry={entryOf(value.name)} /></span>{/if}
    {#if type === 'lora'}{display} <span class="mono muted">{value.weight}</span>{:else if type === 'lora-add'}LoRA{:else if ghost}{display}{:else}<span class="mono">{display}</span>{/if}
    {#if flag}<i class="flag" aria-hidden="true"></i>{/if}
  </button>
  {#if locked && lockable}
    <button class="lk-btn" onclick={onunlock} aria-label="Unlock {label || display}" title="Unlock"><Icon name="lock" /></button>
  {/if}
  {#if tip && !open}
    <span class="tip" aria-hidden="true"><b>{tip[0]}</b>{tip[1]}</span>
  {/if}

  {#if open}
    <div class="pop" class:right transition:pop role="dialog" aria-label="Edit {label || display}">
    <form class="popform" use:focusFirst onsubmit={save}>
      {#if type === 'number' || type === 'count'}
        <label class="lbl-row"><span>{type === 'count' ? 'Images in this job' : label}</span>
          <input class="field num" type="number" {min} {max} {step} bind:value={d.v} /></label>
      {:else if type === 'select'}
        <label class="lbl-row"><span>{label}</span>
          {#if options.length}
            <select class="select" bind:value={d.v}>
              {#each options.includes(d.v) ? options : [d.v, ...options] as o}<option value={o}>{o}</option>{/each}
            </select>
          {:else}
            <input class="field" bind:value={d.v} spellcheck="false" />
          {/if}</label>
      {:else if type === 'size'}
        <div class="lbl-row"><span>Size</span>
          <div class="presets">
            {#each SIZES as [w, h]}
              <button type="button" class="preset" aria-pressed={sizeActive(w, h)} onclick={() => (d = { w, h })}>{w}×{h}</button>
            {/each}
          </div></div>
        <div class="lbl-row"><span>Custom</span>
          <div class="pair"><input class="field num" type="number" min="64" max="4096" step="8" aria-label="Width" bind:value={d.w} />×<input class="field num" type="number" min="64" max="4096" step="8" aria-label="Height" bind:value={d.h} /></div></div>
      {:else if type === 'lora'}
        <h4>{value.name}</h4>
        <label class="lbl-row"><span>Weight</span><input class="field num" type="number" min="-2" max="2" step="0.05" bind:value={d.weight} /></label>
      {:else if type === 'lora-add'}
        {#if options.length}
          <fieldset class="lpick">
            <legend>LoRA</legend>
            <div class="lpick-list">
              {#each options as o (o)}
                <label class="lpick-row" class:on={d.name === o}>
                  <input class="sr" type="radio" name="lora-pick" value={o} bind:group={d.name} />
                  <span class="lcm lcm-lg"><LoraCover entry={entryOf(o)} /></span>
                  <span class="lpick-name" title={o}>{o.replace(/\.(safetensors|ckpt|pt)$/i, '')}</span>
                </label>
              {/each}
            </div>
          </fieldset>
        {:else}
          <label class="lbl-row"><span>LoRA</span>
            <input class="field" placeholder="file name.safetensors" bind:value={d.name} spellcheck="false" /></label>
        {/if}
        <label class="lbl-row"><span>Weight</span><input class="field num" type="number" min="-2" max="2" step="0.05" bind:value={d.weight} /></label>
      {/if}

      <div class="pop-foot">
        {#if lockable && type !== 'lora-add'}
          <span class="lockrow"><button type="button" class="switch" role="switch" aria-checked={lock} aria-label="Lock this value" onclick={() => (lock = !lock)}></button>Lock</span>
        {/if}
        <span class="spacer"></span>
        {#if type === 'lora'}<button type="button" class="btn sm danger" onclick={() => { open = false; onremove() }}>Remove</button>{/if}
        <button type="submit" class="btn sm">{type === 'lora-add' ? 'Add' : 'Save'}</button>
      </div>
    </form>
    </div>
  {/if}
</span>

<style>
  .popform { display: grid; gap: 12px; }
  .chip.ghost { background: transparent; color: var(--muted); padding: 0 8px; }
  .chip.ghost:hover, .chip.ghost.open { background: var(--raised); color: var(--text); }
  .lcm { position: relative; flex: none; width: 20px; height: 20px; border-radius: 5px; overflow: hidden; margin-left: -4px; box-shadow: 0 0 0 1px var(--line2); }
  .lcm :global(svg.i) { width: 12px; height: 12px; }
  .lcm-lg { width: 30px; height: 30px; margin-left: 0; }
  .lcm-lg :global(svg.i) { width: 16px; height: 16px; }
  .lpick { border: 0; margin: 0; padding: 0; min-width: 0; display: grid; gap: 6px; }
  .lpick legend { padding: 0; margin-bottom: 6px; font-size: 12.5px; font-weight: 500; color: var(--muted); }
  .lpick-list { display: grid; gap: 2px; max-height: 220px; overflow-y: auto; margin: 0 -4px; padding: 0 4px; }
  .lpick-row { display: flex; align-items: center; gap: 10px; padding: 3px 6px; border-radius: var(--r-md); border: 1px solid transparent; cursor: pointer; font-size: 13px; }
  .lpick-row:hover { background: var(--raised); }
  .lpick-row.on { background: var(--mint-dim); border-color: var(--mint-line); }
  .lpick-row:has(input:focus-visible) { outline: 2px solid var(--mint); outline-offset: 1px; }
  .lpick-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .flag { width: 6px; height: 6px; border-radius: 50%; background: var(--amber); flex: none; margin-left: -1px; }
</style>
