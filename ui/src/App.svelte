<script>
  import { onMount } from 'svelte'
  import Icon from './Icon.svelte'
  import Logo from './Logo.svelte'
  import Plan from './pages/Plan.svelte'
  import Queue from './pages/Queue.svelte'
  import Gallery from './pages/Gallery.svelte'
  import Library from './pages/Library.svelte'
  import Settings from './pages/Settings.svelte'
  import Guide from './pages/Guide.svelte'
  import { route, boot, activeCount, toasts, dismiss } from './store.svelte.js'
  import { pop } from './motion.js'

  const nav = [
    ['plan', 'Plan', 'plan'],
    ['queue', 'Queue', 'queue'],
    ['gallery', 'Gallery', 'gallery'],
    ['library', 'Library', 'library'],
  ]

  onMount(boot)
  // The wheel over a focused number field would change its value; let it scroll the page instead.
  const wheel = (e) => {
    if (e.target instanceof HTMLInputElement && e.target.type === 'number' && e.target === document.activeElement) e.target.blur()
  }
  const pages = { plan: Plan, queue: Queue, gallery: Gallery, library: Library, how: Guide, settings: Settings }
  const Page = $derived(pages[route.name])
</script>

<svelte:window onwheel={wheel} />

<div class="shell">
  <nav class="rail" aria-label="Main">
    <a class="logo" href="#/plan" aria-label="Comfy Planner"><Logo /><span class="word">Comfy Planner</span></a>
    {#each nav as [key, label, icon]}
      <a class="nav" class:on={route.name === key} href="#/{key}" aria-label={label} aria-current={route.name === key ? 'page' : undefined}>
        <Icon name={icon} /><span class="lbl">{label}</span>
        {#if key === 'queue' && activeCount() > 0}<span class="qcount" title="{activeCount()} in the queue"><span class="dot"></span><span class="num">{activeCount()}</span></span>{/if}
      </a>
    {/each}
    <span class="grow"></span>
    <a class="nav" class:on={route.name === 'how'} href="#/how" aria-label="How it works" aria-current={route.name === 'how' ? 'page' : undefined}>
      <Icon name="book" /><span class="lbl">How it works</span>
    </a>
    <a class="nav" class:on={route.name === 'settings'} href="#/settings" aria-label="Settings" aria-current={route.name === 'settings' ? 'page' : undefined}>
      <Icon name="settings" /><span class="lbl">Settings</span>
    </a>
  </nav>

  <Page />
</div>

<div class="toasts" role="status" aria-live="polite">
  {#each toasts as t (t.id)}
    <div class="toast" class:err={t.kind === 'err'} transition:pop={{ y: 8, scale: 0.96 }}>
      <Icon name={t.kind === 'err' ? 'alert' : 'check'} />
      <span>{t.text}</span>
      {#if t.action}<button class="act" onclick={() => { t.action.run(); dismiss(t.id) }}>{t.action.label}</button>{/if}
    </div>
  {/each}
</div>
