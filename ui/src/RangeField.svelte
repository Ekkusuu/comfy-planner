<script>
  // Recommended / min / max numbers for one setting.
  let { value = $bindable({}), step = 'any' } = $props()
  const set = (key, e) => {
    const raw = e.currentTarget.value
    value = { ...value, [key]: raw === '' || Number.isNaN(+raw) ? null : +raw }
  }
</script>

<div class="range">
  {#each [['rec', 'Recommended'], ['min', 'Min'], ['max', 'Max']] as [key, label]}
    <label><span>{label}</span><input class="field num" type="number" {step} value={value?.[key] ?? ''} oninput={(e) => set(key, e)} /></label>
  {/each}
</div>

<style>
  .range { display: flex; gap: 10px; flex-wrap: wrap; }
  label { display: grid; gap: 4px; font-size: 11.5px; color: var(--muted); font-weight: 500; }
  .field { width: 92px; }
</style>
