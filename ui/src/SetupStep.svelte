<script>
  // One numbered step of the setup page: a number (a check once the step is done), a title, a line of explanation and the step's content.
  import Icon from './Icon.svelte'

  let { n, title, done = false, lede = '', children } = $props()
</script>

<section class="step" aria-labelledby="setup-step-{n}">
  <span class="num" class:done aria-hidden="true">{#if done}<Icon name="check" />{:else}{n}{/if}</span>
  <div class="body">
    <div class="gh">
      <h2 id="setup-step-{n}">{title}{#if done}<span class="sr"> (done)</span>{/if}</h2>
      {#if lede}<p>{lede}</p>{/if}
    </div>
    {@render children()}
  </div>
</section>

<style>
  .step { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 14px; align-items: start; }
  .num { margin-top: 1px; width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--line2); color: var(--muted); display: grid; place-items: center; font-size: 12px; font-weight: 600; transition: background 0.3s var(--ease), border-color 0.3s var(--ease), color 0.3s var(--ease); }
  .num.done { background: var(--mint-dim); border-color: var(--mint-line); color: var(--mint); }
  .num :global(svg.i) { width: 13px; height: 13px; }
  .body { min-width: 0; display: grid; gap: 12px; align-content: start; }
  .gh { display: grid; gap: 3px; }
  .gh h2 { font-size: 15px; font-weight: 600; line-height: 26px; }
  .gh p { color: var(--muted); font-size: 13px; line-height: 1.5; max-width: 64ch; }
</style>
