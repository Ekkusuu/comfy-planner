<script>
  import { onMount } from 'svelte'
  import { api } from '../api.js'
  import { live, toast, toastError } from '../store.svelte.js'
  import { pop } from '../motion.js'
  import Icon from '../Icon.svelte'
  import InfoTip from '../InfoTip.svelte'
  import SettingRow from '../SettingRow.svelte'
  import TagInput from '../TagInput.svelte'

  const KEYS = [
    'comfy_url', 'comfy_install_dir', 'comfy_cli',
    'max_images_per_job', 'max_images_per_request', 'max_loras', 'hires_default', 'safety_negative', 'strip_from_default_negative',
    'llm_model', 'llm_models', 'llm_ctx_size', 'llm_binary', 'llm_gpu_layers', 'llm_enable_thinking', 'llm_temperature',
    'llm_max_tokens', 'llm_start_timeout', 'llm_stop_timeout', 'llm_extra_args',
    'allow_draft_cards',
    'wildcards_enabled', 'wildcards_llm_picks_lists', 'wildcards_ideas_per_prompt', 'wildcards_entries_per_list', 'embeddings_enabled',
  ]

  let server = $state(null) // the config as the app reports it
  let base = $state(null) // editable values as last saved
  let d = $state(null) // what's in the form
  let errors = $state({})
  let saving = $state(false)
  let advanced = $state(false)

  const pick = (c) => ({ ...Object.fromEntries(KEYS.map((k) => [k, $state.snapshot(c[k])])), llm_models: Object.fromEntries(c.llm_models.map((m) => [m.name, m.path])) })
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)
  const dirty = $derived(d ? KEYS.filter((k) => !same(d[k], base[k])) : [])
  // an error stays until the field it belongs to is edited
  const err = (k) => (errors[k] && JSON.stringify(d?.[k]) === errors[k].at ? errors[k].msg : '')
  const bad = $derived(Object.keys(errors).filter(err))
  const isNew = (k) => dirty.includes(k)

  function load(c) {
    server = c
    base = pick(c)
    d = $state.snapshot(base)
    errors = {}
  }
  onMount(async () => {
    try { load(await api.config()) } catch (e) { toastError(e) }
  })

  async function save() {
    saving = true
    errors = {}
    try {
      const res = await api.saveConfig(Object.fromEntries(dirty.map((k) => [k, $state.snapshot(d[k])])))
      load(res)
      live.config = { ...live.config, ...res }
      toast('Settings saved')
    } catch (e) {
      if (e.fields) {
        errors = Object.fromEntries(Object.entries(e.fields).map(([k, msg]) => [k, { msg, at: JSON.stringify(d[k]) }]))
        if (Object.keys(e.fields).some((k) => ['llm_binary', 'llm_models', 'llm_start_timeout', 'llm_stop_timeout', 'llm_gpu_layers', 'llm_temperature', 'llm_max_tokens', 'llm_extra_args'].includes(k))) advanced = true
      } else toastError(e)
    } finally {
      saving = false
    }
  }
  function onkey(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 's') {
      e.preventDefault()
      if (dirty.length && !saving) save()
    }
  }
  function discard() {
    d = $state.snapshot(base)
    errors = {}
  }

  const launch = $derived(server && [
    ['App address', `http://${server.host}:${server.port}`],
    ['Data folder', server.data_dir],
    ['Images', server.images_dir],
    ['Workflow copies', server.workflows_dir],
    ['LLM server', server.llm_url],
  ])
  // The agent address: /mcp on the app's own address. A wildcard bind address is not one an agent can call.
  const mcpUrl = $derived(server && `http://${['0.0.0.0', '::', ''].includes(server.host) ? '127.0.0.1' : server.host}:${server.port}/mcp`)
  const mcpJson = $derived(`{\n  "mcpServers": {\n    "comfy-planner": { "type": "http", "url": "${mcpUrl}" }\n  }\n}`)
  let showJson = $state(false)
  let copied = $state('') // which snippet was just copied
  let copiedTimer
  async function copy(key, text) {
    try {
      try {
        await navigator.clipboard.writeText(text)
      } catch {
        const t = document.createElement('textarea')
        t.value = text
        t.style.cssText = 'position:fixed;opacity:0'
        document.body.append(t)
        t.select()
        const ok = document.execCommand('copy')
        t.remove()
        if (!ok) throw new Error('copy failed')
      }
    } catch {
      toast("Couldn't copy. Select the text and copy it by hand.", { kind: 'err' })
      return
    }
    copied = key
    clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => (copied = ''), 1600)
  }
  const modelNames = $derived(d ? Object.keys(d.llm_models) : [])
  const chosen = $derived(server?.llm_models.find((m) => m.name === d?.llm_model))
  const noModel = $derived(!!server && !chosen?.found) // the planner has no model file to start
  let addModel = $state(false)
  let opening = $state(false)
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
</script>

<svelte:window onkeydown={onkey} />

{#snippet snip(key, text, label)}
  <div class="code">
    <pre class="mono" role="region" aria-label={label}>{text}</pre>
    <button class="icon-btn sm" class:done={copied === key} onclick={() => copy(key, text)} aria-label={copied === key ? 'Copied' : `Copy ${label}`} title={copied === key ? 'Copied' : 'Copy'}>
      <Icon name={copied === key ? 'check' : 'copy'} />
    </button>
  </div>
{/snippet}

{#snippet modelSteps()}
  <ol class="steps">
    <li>Download a language model in GGUF format, the kind llama.cpp can run. Choose one that fits your graphics card's memory.</li>
    <li>Put the file in the models folder.</li>
    <li>Open Advanced model settings and enter the file's path next to a model name, like <span class="mono">models/your-model.gguf</span>.</li>
    <li>Choose that name in Model, then save.</li>
  </ol>
{/snippet}

{#snippet openFolder()}
  <button class="btn" onclick={openModels} disabled={opening}><Icon name={opening ? 'loader' : 'folder'} />Open models folder</button>
{/snippet}

<main class="page" style="max-width:860px">
  <div class="head"><h1>Settings</h1></div>

  <p class="note">Changes apply as soon as you save, no restart needed. They are written to <span class="mono">{server?.config_file ?? 'config/app.toml'}</span> with its comments kept.</p>

  {#if !d}
    <div class="skeleton" aria-hidden="true"></div>
  {:else}
    <section class="stack">
      <div class="gh"><h2>ComfyUI</h2><p>The image engine this app drives.</p></div>
      <div class="card">
        <SettingRow label="Address" id="s-url" changed={isNew('comfy_url')} error={err('comfy_url')}
          tip="Where ComfyUI is listening. The default is right unless you changed ComfyUI's port.">
          <input class="field mono" id="s-url" bind:value={d.comfy_url} spellcheck="false" />
        </SettingRow>
        <SettingRow label="Install folder" id="s-dir" changed={isNew('comfy_install_dir')} error={err('comfy_install_dir')}
          tip="The ComfyUI folder that holds models, custom_nodes and user. The app reads LoRA files and your saved workflows from here and never changes your install.">
          <input class="field mono" id="s-dir" bind:value={d.comfy_install_dir} placeholder="Not set" spellcheck="false" />
        </SettingRow>
        <SettingRow label="Start command" id="s-cli" changed={isNew('comfy_cli')} error={err('comfy_cli')}
          tip="What the Start ComfyUI button runs when ComfyUI is off. “comfy” is the comfy-cli tool.">
          <input class="field mono" id="s-cli" bind:value={d.comfy_cli} spellcheck="false" style="max-width:240px" />
        </SettingRow>
      </div>
    </section>

    <section class="stack">
      <div class="gh"><h2>Jobs</h2><p>Limits and defaults for new jobs.</p></div>
      <div class="card">
        <SettingRow label="Images per job, at most" id="s-max" changed={isNew('max_images_per_job')} error={err('max_images_per_job')}
          tip="The most images one job can ask for. It stops a typo from queueing hundreds.">
          <input class="field num" id="s-max" type="number" min="1" max="100" step="1" bind:value={d.max_images_per_job} style="width:110px" />
        </SettingRow>
        <SettingRow label="Images per request, at most" id="s-req" changed={isNew('max_images_per_request')} error={err('max_images_per_request')}
          tip="The most images one request on the Plan page may add up to, however it is split. A request for 100 images becomes 100 images, each planned on its own, and this caps a typo like 10000.">
          <input class="field num" id="s-req" type="number" min="1" max="2000" step="1" bind:value={d.max_images_per_request} style="width:110px" />
        </SettingRow>
        <SettingRow label="LoRAs per job, at most" id="s-loras" changed={isNew('max_loras')} error={err('max_loras')}
          tip="A LoRA is a small add-on model that changes style or adds a character. This is how many one job may combine; more tends to muddy the result.">
          <input class="field num" id="s-loras" type="number" min="0" max="10" step="1" bind:value={d.max_loras} style="width:110px" />
        </SettingRow>
        <SettingRow label="Hires pass on by default" id="s-hires" changed={isNew('hires_default')} error={err('hires_default')}
          tip="Hires redraws each image larger for more detail and takes longer. This is what new jobs start with; you can change it per job.">
          <button class="switch" id="s-hires" role="switch" aria-checked={d.hires_default} aria-label="Hires pass on by default" onclick={() => (d.hires_default = !d.hires_default)}></button>
        </SettingRow>
        <SettingRow label="Standing negative tags" id="s-neg" changed={isNew('safety_negative')} error={err('safety_negative')}
          hint={d.safety_negative.length ? '' : 'None. The app adds nothing to your negative prompts.'}
          tip="Tags added to every negative prompt (what you don't want in the image), after the planner and your own edits.">
          <TagInput bind:value={d.safety_negative} label="Standing negative tags" placeholder="Add a tag and press Enter" invalid={!!err('safety_negative')} />
        </SettingRow>
        <SettingRow label="Removed from workflow negatives" id="s-strip" changed={isNew('strip_from_default_negative')} error={err('strip_from_default_negative')}
          tip="Workflows ship with their own negative prompt. These tags are left out when the app uses it. Your workflow files stay as they are, and tags you type into a job are always kept.">
          <TagInput bind:value={d.strip_from_default_negative} label="Tags removed from workflow negatives" placeholder="Add a tag and press Enter" invalid={!!err('strip_from_default_negative')} />
        </SettingRow>
      </div>
    </section>

    <section class="stack">
      <div class="gh"><h2>Local LLM</h2><p>The model that plans your jobs and writes cards. It runs only while planning, then makes room for ComfyUI. Changes apply the next time it starts.</p></div>
      {#if noModel}
        <div class="banner setup" role="status">
          <Icon name="alert" />
          <div class="tx">
            <b>Download a language model to start planning</b>
            {@render modelSteps()}
          </div>
          {@render openFolder()}
        </div>
      {/if}
      <div class="card">
        <SettingRow label="Model" id="s-model" changed={isNew('llm_model')} error={err('llm_model')}
          hint={chosen ? (chosen.found ? chosen.path : `File not found: ${chosen.path}`) : ''}
          tip="Which of your local models does the planning. Add or change model files under Advanced.">
          <select class="select" id="s-model" bind:value={d.llm_model} style="min-width:200px">
            {#each modelNames as name}<option value={name}>{name}</option>{/each}
          </select>
        </SettingRow>
        <SettingRow label="Context size" id="s-ctx" changed={isNew('llm_ctx_size')} error={err('llm_ctx_size')}
          hint="Tokens, roughly ¾ of a word each."
          tip="How much text the model can hold in mind at once. Larger fits more of your library into one plan but uses more video memory.">
          <input class="field num" id="s-ctx" type="number" min="512" step="512" bind:value={d.llm_ctx_size} style="width:130px" />
        </SettingRow>
      </div>

      {#if !noModel}
        <div>
          <button class="more" aria-expanded={addModel} onclick={() => (addModel = !addModel)}>Add another model<Icon name="chevron" /></button>
          <div class="details" class:open={addModel}>
            <div>
              <div class="setup quiet">
                {@render modelSteps()}
                {@render openFolder()}
              </div>
            </div>
          </div>
        </div>
      {/if}

      <button class="more" aria-expanded={advanced} onclick={() => (advanced = !advanced)}>Advanced model settings<Icon name="chevron" /></button>
      <div class="details" class:open={advanced}>
        <div>
          <div class="card inset">
            <SettingRow label="Model files" id="s-files" changed={isNew('llm_models')} error={err('llm_models')}
              tip="The .gguf file for each model name. A relative path starts in the project folder.">
              <div class="files">
                {#each modelNames as name}
                  <span class="fname mono">{name}</span>
                  <input class="field mono" aria-label="File for {name}" bind:value={d.llm_models[name]} spellcheck="false" />
                {/each}
              </div>
            </SettingRow>
            <SettingRow label="llama-server" id="s-bin" changed={isNew('llm_binary')} error={err('llm_binary')}
              hint={server.llm_binary_found || isNew('llm_binary') ? '' : 'File not found.'}
              tip="The llama.cpp program that loads and runs the model.">
              <input class="field mono" id="s-bin" bind:value={d.llm_binary} spellcheck="false" />
            </SettingRow>
            <SettingRow label="Layers on the GPU" id="s-gpu" changed={isNew('llm_gpu_layers')} error={err('llm_gpu_layers')}
              tip="How many of the model's layers run on the graphics card. 99 means all of them, which is fastest.">
              <input class="field num" id="s-gpu" type="number" min="0" max="999" step="1" bind:value={d.llm_gpu_layers} style="width:110px" />
            </SettingRow>
            <SettingRow label="Let the model think first" id="s-think" changed={isNew('llm_enable_thinking')} error={err('llm_enable_thinking')}
              tip="Lets the model reason before it answers. Slower, and planning is faster and more reliable with it off.">
              <button class="switch" id="s-think" role="switch" aria-checked={d.llm_enable_thinking} aria-label="Let the model think first" onclick={() => (d.llm_enable_thinking = !d.llm_enable_thinking)}></button>
            </SettingRow>
            <SettingRow label="Temperature" id="s-temp" changed={isNew('llm_temperature')} error={err('llm_temperature')}
              tip="How adventurous the model is, from 0 to 2. Low values are steady and repeatable; higher ones are more varied.">
              <input class="field num" id="s-temp" type="number" min="0" max="2" step="0.05" bind:value={d.llm_temperature} style="width:110px" />
            </SettingRow>
            <SettingRow label="Longest reply" id="s-tokens" changed={isNew('llm_max_tokens')} error={err('llm_max_tokens')}
              hint="Tokens"
              tip="The most the model may write in one answer. Raise it if plans come back cut off.">
              <input class="field num" id="s-tokens" type="number" min="64" step="100" bind:value={d.llm_max_tokens} style="width:130px" />
            </SettingRow>
            <SettingRow label="Wait for it to start" id="s-start" changed={isNew('llm_start_timeout')} error={err('llm_start_timeout')}
              hint="Seconds"
              tip="How long to wait for the model to finish loading before giving up. Big models on a slow disk need longer.">
              <input class="field num" id="s-start" type="number" min="10" step="10" bind:value={d.llm_start_timeout} style="width:110px" />
            </SettingRow>
            <SettingRow label="Wait for it to stop" id="s-stop" changed={isNew('llm_stop_timeout')} error={err('llm_stop_timeout')}
              hint="Seconds"
              tip="How long to wait for the model to quit by itself before it is closed by force.">
              <input class="field num" id="s-stop" type="number" min="1" step="1" bind:value={d.llm_stop_timeout} style="width:110px" />
            </SettingRow>
            <SettingRow label="Extra options" id="s-extra" changed={isNew('llm_extra_args')} error={err('llm_extra_args')}
              tip="Anything else to pass to llama-server, one item per entry, like --flash-attn. For advanced tuning only.">
              <TagInput bind:value={d.llm_extra_args} label="Extra llama-server options" placeholder="Add an option and press Enter" invalid={!!err('llm_extra_args')} />
            </SettingRow>
          </div>
        </div>
      </div>
    </section>

    <section class="stack">
      <div class="gh"><h2>Cards</h2><p>Short notes about each workflow and LoRA that the planner reads.</p></div>
      <div class="card">
        <SettingRow label="Planner may use draft cards" id="s-draft" changed={isNew('allow_draft_cards')} error={err('allow_draft_cards')}
          hint={d.allow_draft_cards ? 'Drafts are used too.' : 'Approved cards only.'}
          tip="A draft is a card the LLM wrote that you haven't checked. Off keeps the planner to cards you approved in the Library, so it only acts on notes you trust.">
          <button class="switch" id="s-draft" role="switch" aria-checked={d.allow_draft_cards} aria-label="Planner may use draft cards" onclick={() => (d.allow_draft_cards = !d.allow_draft_cards)}></button>
        </SettingRow>
        <SettingRow label="Civitai token" id="s-civ"
          tip="Lets the Library look up your LoRAs on Civitai by file hash to fill in trigger words and descriptions. It lives in the CIVITAI_API_TOKEN environment variable. The app never shows or stores it.">
          <span class="found" id="s-civ"><span class="dot" class:off={!server.civitai_token_found}></span>{server.civitai_token_found ? 'Found' : 'Not found'}</span>
          {#if !server.civitai_token_found}<p class="hint">Set CIVITAI_API_TOKEN in your Windows environment and restart the app. Without it, LoRA cards are built from the file's own metadata.</p>{/if}
        </SettingRow>
      </div>
    </section>

    <section class="stack">
      <div class="gh"><h2>Wildcards</h2><p>Lists of ideas you imported in the Library. How the planner uses them when it writes prompts.</p></div>
      <div class="card">
        <SettingRow label="Offer wildcard ideas to the planner" id="s-wc" changed={isNew('wildcards_enabled')} error={err('wildcards_enabled')}
          hint={d.wildcards_enabled ? 'Ideas are offered.' : 'Off. Typing __list-name__ into a request still works.'}
          tip="When on, the app draws random lines from your wildcard lists that suit the request and shows them to the LLM as optional ingredients. It uses the ones that fit, so your images vary more. Off, the LLM writes prompts on its own.">
          <button class="switch" id="s-wc" role="switch" aria-checked={d.wildcards_enabled} aria-label="Offer wildcard ideas to the planner" onclick={() => (d.wildcards_enabled = !d.wildcards_enabled)}></button>
        </SettingRow>
        <SettingRow label="Let the LLM choose which lists to use" id="s-wcp" changed={isNew('wildcards_llm_picks_lists')} error={err('wildcards_llm_picks_lists')}
          hint={d.wildcards_llm_picks_lists ? 'One short extra LLM call per job.' : 'Off. Lists are chosen by matching words only.'}
          tip="When on, the LLM reads a short catalog of your lists (name, size, a few sample lines) and picks the ones that suit the request, even when the list name doesn't say so. Off, the app only matches the request against list names and entries it can spot as a whole phrase, which is faster but misses synonyms and vague requests like “surprise me”.">
          <button class="switch" id="s-wcp" role="switch" aria-checked={d.wildcards_llm_picks_lists} aria-label="Let the LLM choose which lists to use" onclick={() => (d.wildcards_llm_picks_lists = !d.wildcards_llm_picks_lists)}></button>
        </SettingRow>
        <SettingRow label="Search by meaning" id="s-emb" changed={isNew('embeddings_enabled')} error={err('embeddings_enabled')}
          hint={!d.embeddings_enabled ? 'Off. Lists, LoRAs and liked examples are found by matching words only.' : server?.embeddings_model_found ? 'On. A small model runs on the CPU, not the GPU.' : 'On, but the model file was not found, so words only.'}
          tip="When on, a small embedding model (it runs on the CPU, about 800 MB of memory while in use, and stops when idle) lets the planner find wildcard lists, LoRA cards and your liked prompts by meaning: “moody” finds a list holding “melancholy”, “steampunk” finds brass gears. Off, or if the model file is missing, everything falls back to matching words.">
          <button class="switch" id="s-emb" role="switch" aria-checked={d.embeddings_enabled} aria-label="Search by meaning" onclick={() => (d.embeddings_enabled = !d.embeddings_enabled)}></button>
        </SettingRow>
        <SettingRow label="Reranking model" id="s-rr"
          tip="An optional second small model (CPU only) that reads your request together with the best matches of the search by meaning and puts them in better order. It is not shipped with the app: put its file in the models folder, at the path set under [reranker] in config/app.toml, and set enabled = true there. Off by default: the search by meaning works on its own.">
          <span class="found" id="s-rr"><span class="dot" class:off={!server.reranker_enabled || !server.reranker_model_found}></span>{!server.reranker_enabled ? 'Off' : server.reranker_model_found ? 'Found' : 'Not found'}</span>
          {#if !server.reranker_enabled || !server.reranker_model_found}<p class="hint">Optional. The search by meaning is used on its own.</p>{/if}
        </SettingRow>
        <SettingRow label="Lists per prompt" id="s-wcl" changed={isNew('wildcards_ideas_per_prompt')} error={err('wildcards_ideas_per_prompt')}
          tip="How many different lists the ideas for one prompt come from: the lists that match your request best, then open slots like the setting, camera shot, outfit or pose. 0 offers none.">
          <input class="field num" id="s-wcl" type="number" min="0" max="10" step="1" bind:value={d.wildcards_ideas_per_prompt} style="width:110px" />
        </SettingRow>
        <SettingRow label="Ideas per list" id="s-wce" changed={isNew('wildcards_entries_per_list')} error={err('wildcards_entries_per_list')}
          hint="Large jobs get fewer so the planner’s prompt stays short."
          tip="How many random lines are drawn from each chosen list, for each prompt. Every prompt gets its own draw, which is what makes the images different.">
          <input class="field num" id="s-wce" type="number" min="1" max="30" step="1" bind:value={d.wildcards_entries_per_list} style="width:110px" />
        </SettingRow>
      </div>
    </section>

    <section class="stack">
      <div class="gh"><h2>AI agents (MCP)</h2>
        <p>MCP is a standard way for AI agents to use other programs. Connect an agent and it can plan and generate images, edit pictures, manage the queue, and change settings and cards.</p></div>
      <div class="card">
        <SettingRow label="Address" id="s-mcp-url"
          tip="Where agents connect. It is the app address plus /mcp, so it follows the port set in config/app.toml.">
          {@render snip('url', mcpUrl, 'agent address')}
        </SettingRow>
        <SettingRow label="JSON config" id="s-mcp-json"
          hint="For agents that read their MCP servers from a settings file."
          tip="Add this to the agent's MCP configuration. The server name can be anything; comfy-planner is a suggestion.">
          <button class="more flat" id="s-mcp-json" aria-expanded={showJson} onclick={() => (showJson = !showJson)}>{showJson ? 'Hide' : 'Show'} the JSON config<Icon name="chevron" /></button>
          <div class="details" class:open={showJson}>
            <div>{@render snip('json', mcpJson, 'JSON config')}</div>
          </div>
        </SettingRow>
      </div>
      <p class="limits">Agents can only connect from this computer, and the app has to be running. They can't delete anything, import workflows, download models, or change the settings that decide which programs run or where ComfyUI is.</p>
    </section>

    <section class="stack">
      <div class="gh"><h2>Set when the app starts</h2>
        <p>These are tied to how the app is launched, so they can't be edited here. Change them in config/app.toml and restart. <InfoTip text="The launcher opens the app on this address and keeps your data in this folder. Changing them live would leave windows and files pointing at the old place." label="Why can't I edit these?" /></p></div>
      <dl class="dl cfg">
        {#each launch as [k, v]}<dt>{k}</dt><dd class="mono">{v}</dd>{/each}
      </dl>
    </section>
  {/if}

  {#if dirty.length || bad.length}
    <div class="savebar" role="region" aria-label="Unsaved changes" transition:pop={{ y: 10, scale: 0.98 }}>
      {#if bad.length}
        <span class="msg err"><Icon name="alert" />Fix {bad.length === 1 ? 'the highlighted setting' : `${bad.length} highlighted settings`} to save</span>
      {:else}
        <span class="msg">{dirty.length} unsaved {dirty.length === 1 ? 'change' : 'changes'}</span>
      {/if}
      <span class="spacer"></span>
      <button class="btn" onclick={discard} disabled={saving}>Discard</button>
      <button class="btn primary" onclick={save} disabled={saving || !dirty.length} title="Save (Ctrl+S)">{#if saving}<Icon name="loader" /> {/if}Save changes</button>
    </div>
  {/if}
</main>

<style>
  .note { color: var(--muted); max-width: 64ch; margin-top: -12px; }
  .note .mono { color: var(--text-soft); overflow-wrap: anywhere; }
  .gh { display: grid; gap: 3px; }
  .gh h2 { font-size: 15px; font-weight: 600; }
  .gh p { color: var(--muted); font-size: 13px; max-width: 64ch; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); }
  .card.inset { margin: 2px 0 4px; }
  .steps { margin: 8px 0 0; padding-left: 18px; display: grid; gap: 4px; color: var(--text-soft); font-size: 13px; line-height: 1.5; max-width: 64ch; }
  .steps li::marker { color: var(--muted); }
  .banner .tx .steps .mono { display: inline; color: var(--text); font-size: 12.5px; overflow-wrap: anywhere; }
  .banner.setup { align-items: flex-start; }
  .banner.setup > svg.i { margin-top: 2px; }
  .banner.setup .tx { flex: 1 1 320px; }
  .setup.quiet { display: grid; justify-items: start; gap: 12px; padding: 4px 16px 16px; margin-top: 4px; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); }
  .more { width: fit-content; }
  .more.flat { height: 32px; }
  .details { width: 100%; }
  .code { display: flex; align-items: flex-start; gap: 6px; width: 100%; min-width: 0; padding: 4px 4px 4px 12px; background: var(--bg); border: 1px solid var(--line); border-radius: var(--r-md); }
  .code pre { flex: 1; min-width: 0; margin: 0; padding: 4px 0; line-height: 20px; color: var(--text-soft); white-space: pre-wrap; overflow-wrap: anywhere; user-select: all; }
  .code .icon-btn.done { color: var(--mint); }
  .limits { color: var(--muted); font-size: 12.5px; line-height: 1.45; max-width: 64ch; }
  .files { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 12px; align-items: center; width: 100%; }
  .fname { color: var(--muted); }
  .found { display: inline-flex; align-items: center; gap: 8px; height: 32px; color: var(--text-soft); }
  .dl.cfg { grid-template-columns: 200px minmax(0, 1fr); padding: 0; background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-xl); gap: 0; }
  .dl.cfg dt, .dl.cfg dd { padding: 11px 16px; border-top: 1px solid var(--line); }
  .dl.cfg dt:first-of-type, .dl.cfg dt:first-of-type + dd { border-top: 0; }
  .dl.cfg dd { overflow-wrap: anywhere; color: var(--text-soft); }
  .savebar { position: sticky; bottom: 20px; z-index: 20; display: flex; align-items: center; gap: 10px; padding: 10px 10px 10px 16px; background: var(--float); border: 1px solid var(--line2); border-radius: var(--r-xl); box-shadow: var(--shadow-overlay); }
  .savebar .msg { display: inline-flex; align-items: center; gap: 8px; color: var(--text-soft); font-size: 13.5px; }
  .savebar .msg.err { color: var(--red); }
  @media (max-width: 700px) { .dl.cfg { grid-template-columns: 1fr; } .dl.cfg dd { border-top: 0; padding-top: 0; } }
</style>
