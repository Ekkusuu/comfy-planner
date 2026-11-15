<script>
  // First-run setup: find ComfyUI, install what the app needs (one confirm), choose a language model, then import a workflow.
  import { onMount } from 'svelte'
  import { api } from '../api.js'
  import { go, toast, toastError, startComfy, ui, refreshConfig, refreshHealth, dismissSetup } from '../store.svelte.js'
  import { pop } from '../motion.js'
  import Icon from '../Icon.svelte'
  import SettingRow from '../SettingRow.svelte'
  import SetupStep from '../SetupStep.svelte'

  let st = $state(null) // /api/setup/status
  let loadError = $state('')
  let found = $state([]) // ComfyUI folders found on this computer
  let url = $state('')
  let folder = $state('')
  let formReady = false
  let comfyErrors = $state({})
  let savingComfy = $state(false)
  let confirming = $state(false)
  let busy = $state(false)
  let modelName = $state('')
  let modelPath = $state('')
  let nameTouched = false
  let modelErrors = $state({})
  let savingModel = $state(false)
  let opening = $state(false)

  const DOWNLOADS = ['llama_server', 'embeddings_model', 'tag_library']
  const items = $derived(Object.fromEntries((st?.items ?? []).map((i) => [i.id, i])))
  const parts = $derived(DOWNLOADS.map((id) => items[id]).filter(Boolean))
  const running = $derived(st?.install.state === 'running')
  const comfyOk = $derived(items.comfyui?.state === 'ok')
  const partsOk = $derived(parts.length > 0 && parts.every((p) => p.state === 'ok'))
  const modelOk = $derived(items.llm_model?.state === 'ok')
  const failed = $derived(parts.filter((p) => p.state === 'error'))
  const current = $derived(!comfyOk ? 1 : !partsOk ? 2 : !modelOk ? 3 : 4) // the step whose action is the mint button
  const comfyDirty = $derived(!!st && (url !== st.comfy.url || folder !== st.comfy.install_dir))
  const overall = $derived.by(() => {
    let done = 0
    let total = 0
    for (const j of Object.values(st?.install.items ?? {})) if (j.total) { done += j.done; total += j.total }
    return total ? { done, total, pct: Math.min(100, Math.floor((done * 100) / total)) } : null
  })

  const size = (b) => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 1e6))} MB`)

  async function load() {
    try {
      st = await api.setupStatus()
      loadError = ''
      if (!formReady) {
        url = st.comfy.url
        folder = st.comfy.install_dir
        formReady = true
      }
    } catch (e) {
      loadError = e.message
    }
  }

  let timer
  async function tick() {
    await load()
    timer = setTimeout(tick, running ? 700 : 2500)
  }
  let wasRunning = false
  $effect(() => {
    // when an install ends, the rest of the app learns what changed (the llama-server path, the search model)
    if (wasRunning && !running) {
      refreshConfig()
      if (st?.install.state === 'done') toast('Everything needed is installed')
    }
    wasRunning = running
  })
  onMount(() => {
    tick()
    api.setupComfy().then((r) => (found = r.installs)).catch(() => {})
    return () => clearTimeout(timer)
  })

  async function saveComfy(change) {
    savingComfy = true
    comfyErrors = {}
    try {
      st = await api.setupSetComfy(change)
      url = st.comfy.url
      folder = st.comfy.install_dir
      refreshConfig()
      refreshHealth()
      toast('ComfyUI settings saved')
    } catch (e) {
      if (e.fields) comfyErrors = e.fields
      else toastError(e)
    } finally {
      savingComfy = false
    }
  }
  const pickFolder = (path) => {
    folder = path
    saveComfy({ install_dir: path })
  }
  async function start() {
    await startComfy()
    await load()
  }

  async function install() {
    confirming = false
    busy = true
    try {
      st = await api.setupInstall()
    } catch (e) {
      toastError(e)
    } finally {
      busy = false
    }
  }
  async function cancel() {
    try {
      st = await api.setupCancel()
    } catch (e) {
      toastError(e)
    }
  }

  const nameFrom = (path) => path.trim().replace(/^["']|["']$/g, '').split(/[\\/]/).pop().replace(/\.gguf$/i, '').slice(0, 60)
  function pathInput() {
    if (!nameTouched) modelName = nameFrom(modelPath)
  }
  async function saveModel() {
    savingModel = true
    modelErrors = {}
    try {
      st = await api.setupModel(modelName, modelPath)
      refreshConfig()
      toast('Language model saved')
      modelPath = ''
    } catch (e) {
      if (e.fields) modelErrors = e.fields
      else toastError(e)
    } finally {
      savingModel = false
    }
  }
  async function openModels() {
    opening = true
    try {
      await api.openModelsFolder()
    } catch (e) {
      toastError(e)
    } finally {
      opening = false
    }
  }

  function leave(to) {
    dismissSetup()
    go(to)
  }
</script>

<main class="page" style="max-width:860px">
  <div class="head">
    <h1>Set up Comfy Planner</h1>
    <span class="spacer"></span>
    <button class="btn" onclick={() => leave('plan')}>Skip for now</button>
  </div>
  <p class="note">A few things are needed before the app can plan images. Everything stays on this computer, and you can come back to this page from Settings.</p>

  {#if !st}
    {#if loadError}
      <div class="banner err" role="alert"><Icon name="alert" /><div class="tx"><b>Can't read the setup status</b><span>{loadError}</span></div></div>
    {:else}
      <div class="skeleton" aria-hidden="true"></div>
    {/if}
  {:else}
    <div class="steps">
      <SetupStep n={1} title="ComfyUI" done={comfyOk} lede="The image engine this app drives. Comfy Planner doesn't install ComfyUI: install it first, then choose its folder here.">
        <p class="state" class:ok={comfyOk}><span class="dot" class:off={!comfyOk}></span>{items.comfyui.detail}</p>
        {#if found.length && !comfyOk}
          <div class="finds" role="group" aria-label="ComfyUI folders found on this computer">
            <span class="muted">Found on this computer</span>
            {#each found as f}
              <button class="pick" class:on={folder === f.path} onclick={() => pickFolder(f.path)} disabled={savingComfy}><b>{f.kind}</b><span class="mono">{f.path}</span></button>
            {/each}
          </div>
        {/if}
        <div class="card">
          <SettingRow label="Address" id="su-url" error={comfyErrors.comfy_url} tip="Where ComfyUI answers. The usual address is http://127.0.0.1:8188.">
            <input class="field mono" id="su-url" bind:value={url} spellcheck="false" />
          </SettingRow>
          <SettingRow label="ComfyUI folder" id="su-dir" error={comfyErrors.comfy_install_dir} hint="The folder that holds main.py, models and user."
            tip="The app reads your LoRA files and saved workflows from here, and uses it to start ComfyUI. It never changes your install.">
            <input class="field mono" id="su-dir" bind:value={folder} spellcheck="false" placeholder="C:\Users\you\ComfyUI_windows_portable\ComfyUI" />
          </SettingRow>
        </div>
        <div class="acts">
          <button class="btn" onclick={() => saveComfy({ url, install_dir: folder })} disabled={savingComfy || !comfyDirty}>{#if savingComfy}<Icon name="loader" />{/if}Save</button>
          {#if !comfyOk}
            <button class="btn" class:primary={current === 1 && st.comfy.install_dir_ok && !comfyDirty} onclick={start} disabled={ui.starting || !st.comfy.install_dir_ok || comfyDirty}>
              <Icon name={ui.starting ? 'loader' : 'play'} />Start ComfyUI
            </button>
          {/if}
        </div>
      </SetupStep>

      <SetupStep n={2} title="Programs and data" done={partsOk} lede="The language model program, a small search model and the tag library. They are downloaded into the app folder, and each file is checked against a known checksum before it is used.">
        <ul class="parts">
          {#each parts as p (p.id)}
            <li class="part">
              {#if p.state === 'installing'}<Icon name="loader" />
              {:else}<span class="dot" class:off={p.state === 'missing'} class:bad={p.state === 'error'}></span>{/if}
              <div class="tx">
                <b>{p.label}</b>
                <span class:err={p.state === 'error'}>{p.detail}</span>
                {#if p.state === 'installing'}
                  <div class="prog" class:indet={!p.total} role="progressbar" aria-label="{p.label} progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow={p.total ? Math.floor((p.done * 100) / p.total) : undefined}>
                    <i style="width:{p.total ? Math.min(100, (p.done * 100) / p.total) : 35}%"></i>
                  </div>
                  {#if p.total}<span class="mono num">{size(p.done)} of {size(p.total)} · {Math.floor((p.done * 100) / p.total)}%</span>{/if}
                {/if}
              </div>
              {#if p.size_bytes && p.state !== 'ok'}<span class="mono sz">{size(p.size_bytes)}</span>{/if}
            </li>
          {/each}
        </ul>

        {#if running}
          <div class="acts">
            {#if overall}<span class="muted mono">{size(overall.done)} of {size(overall.total)} · {overall.pct}%</span>{/if}
            <span class="spacer"></span>
            <button class="btn" onclick={cancel}>Cancel</button>
          </div>
        {:else if !partsOk}
          {#if confirming}
            <div class="confirm" transition:pop={{ y: 6, scale: 0.98 }}>
              <p>This downloads <b class="mono">{size(st.total_bytes)}</b> from GitHub and Hugging Face into the app folder. You can cancel at any time and continue later.</p>
              <div class="acts">
                <button class="btn primary" onclick={install} disabled={busy}>Download and install</button>
                <button class="btn" onclick={() => (confirming = false)}>Cancel</button>
              </div>
            </div>
          {:else}
            <div class="acts">
              <button class="btn" class:primary={current === 2} onclick={() => (confirming = true)} disabled={busy}>
                <Icon name={failed.length ? 'retry' : 'import'} />{failed.length ? 'Retry' : 'Install everything needed'}
              </button>
              <span class="muted mono">{size(st.total_bytes)} in total</span>
            </div>
          {/if}
          {#if st.install.state === 'cancelled'}<p class="note-s">The install was stopped. What was downloaded is kept, so it continues where it stopped.</p>{/if}
        {/if}
      </SetupStep>

      <SetupStep n={3} title="Language model" done={modelOk} lede="A GGUF file is a language model packaged for llama.cpp. Comfy Planner doesn't download one: choose one you already have, sized to fit your graphics card's memory.">
        {#if modelOk}<p class="state ok"><span class="dot"></span>Using: {items.llm_model.detail}</p>{/if}
        <div class="card">
          <SettingRow label="Model file" id="su-path" error={modelErrors.path} hint="The full path of a .gguf file, or a path inside the app folder like models/your-model.gguf."
            tip="The file stays where it is: the app only remembers its path. To keep your models together, open the models folder and drop the file in.">
            <input class="field mono" id="su-path" bind:value={modelPath} oninput={pathInput} spellcheck="false" placeholder="D:\models\your-model.gguf" />
          </SettingRow>
          <SettingRow label="Name" id="su-name" error={modelErrors.name} hint="How the model is listed in Settings. Any name is fine."
            tip="A short name for this model. You can add several models in Settings and switch between them.">
            <input class="field" id="su-name" bind:value={modelName} oninput={() => (nameTouched = true)} maxlength="60" style="max-width:260px" />
          </SettingRow>
        </div>
        <div class="acts">
          <button class="btn" class:primary={current === 3} onclick={saveModel} disabled={savingModel || !modelPath.trim() || !modelName.trim()}>
            {#if savingModel}<Icon name="loader" />{/if}Use this model
          </button>
          <button class="btn" onclick={openModels} disabled={opening}><Icon name={opening ? 'loader' : 'folder'} />Open models folder</button>
        </div>
      </SetupStep>

      <SetupStep n={4} title="Import your first workflow" lede="Workflows are the recipes you built in ComfyUI. Import one in the Library and the app writes a card for it, so the planner knows how to use it.">
        <div class="acts">
          <button class="btn" onclick={() => leave('library')}><Icon name="library" />Open the Library</button>
          <span class="spacer"></span>
          <button class="btn" class:primary={current === 4} onclick={() => leave('plan')}>Finish</button>
        </div>
      </SetupStep>
    </div>
  {/if}
</main>

<style>
  .note { color: var(--muted); max-width: 64ch; margin-top: -12px; }
  .steps { display: grid; gap: 34px; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); }
  .state { display: flex; align-items: center; gap: 9px; color: var(--text-soft); font-size: 13.5px; line-height: 1.45; }
  .state .dot { flex: none; }
  .acts { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .finds { display: grid; gap: 6px; justify-items: start; font-size: 12.5px; }
  .pick { height: auto; min-height: 30px; padding: 6px 11px; display: inline-flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; text-align: left; }
  .pick b { font-weight: 500; color: var(--text); font-size: 13px; }
  .pick .mono { color: var(--muted); overflow-wrap: anywhere; }
  .pick.on { border-color: var(--mint-line); border-style: solid; }
  .parts { list-style: none; margin: 0; padding: 0; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); }
  .part { display: grid; grid-template-columns: 14px minmax(0, 1fr) auto; gap: 12px; align-items: start; padding: 12px 16px; border-top: 1px solid var(--line); }
  .part:first-child { border-top: 0; }
  .part > :global(svg.i) { margin-top: 3px; width: 14px; height: 14px; color: var(--mint); animation: spin 0.8s linear infinite; }
  .part .dot { margin-top: 6px; }
  .part .dot.bad { background: var(--red); box-shadow: 0 0 0 3px var(--red-dim); }
  .tx { min-width: 0; display: grid; gap: 2px; font-size: 13px; line-height: 1.45; }
  .tx b { font-weight: 500; color: var(--text); }
  .tx span { color: var(--muted); }
  .tx span.err { color: var(--red); }
  .tx .prog { margin-top: 6px; }
  .tx .num { font-size: 12px; margin-top: 2px; }
  .sz { color: var(--muted); padding-top: 1px; }
  .confirm { display: grid; gap: 12px; padding: 14px 16px; background: var(--float); border: 1px solid var(--line2); border-radius: var(--r-xl); }
  .confirm p { color: var(--text-soft); font-size: 13.5px; line-height: 1.5; max-width: 62ch; }
  .confirm b { color: var(--text); font-weight: 500; }
  .note-s { color: var(--muted); font-size: 12.5px; }
</style>
