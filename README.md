# Comfy Planner

**A planner and job queue for your ComfyUI workflows, running entirely on your own PC.**

Comfy Planner makes your [ComfyUI](https://github.com/comfyanonymous/ComfyUI) workflows easier to use. Pick a workflow, describe what you want in plain words, and a local language model plans the jobs: it chooses LoRAs and settings, writes a separate prompt for every image and says why. Code checks every choice before anything is queued, and the queue runs the jobs one after another, so a hundred images can be planned and generated without babysitting.

**[Download the latest version](https://github.com/Ekkusuu/comfy-planner/releases/latest)** · **[Report a problem](https://github.com/Ekkusuu/comfy-planner/issues)**

> **ComfyUI and a language model are not included.** You need a working ComfyUI with the models your workflows use, and a language model in GGUF format sized for your graphics card. Everything else is set up by the app.

## Features

- **Plan from a sentence:** a request is split into jobs, each with its LoRAs, settings, a scene plan and one prompt per image, every choice with a one-line reason.
- **You stay in charge:** you choose the workflow, never the model. Any value you change is locked, and a re-plan redoes everything else.
- **Job queue:** live progress and previews; reorder, pause, cancel and retry.
- **Cards for workflows and LoRAs:** a short description the local model drafts and you approve, so the planner knows what each one is good for. LoRA details and preview pictures come from Civitai by file hash.
- **Gallery:** every image keeps its full recipe and can be reused as a new job.
- **Image editing:** upload pictures and run them through an image-edit workflow.
- **Word lists and tag library:** your wildcard lists and a Danbooru tag library give the planner ideas and catch invalid tags; an optional small embeddings model adds search by meaning.
- **One graphics card for both:** the language model and ComfyUI take turns. The queue pauses between two images while planning runs, then continues.
- **Guided first start:** finds ComfyUI on your PC and downloads llama.cpp, the embeddings model and the tag library, each checked against a pinned SHA-256.
- **Settings page** for everything in the config file, applied without a restart.
- **MCP server:** AI agents can plan, generate and manage the queue through a local endpoint.
- **Tray app:** the app opens in its own window and keeps running in the tray.
- **Updates:** checks GitHub for a new version on request (or at every start, if you turn it on). Installing a new version keeps your settings and data.

## Requirements

- Windows 10 or 11, 64-bit
- An NVIDIA graphics card
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) with the models your workflows use
- A language model in GGUF format

## How to use

1. Download `ComfyPlanner-Setup-<version>.exe` from [Releases](https://github.com/Ekkusuu/comfy-planner/releases/latest) and run it. No administrator rights needed.
2. Start **Comfy Planner**. The first time, a short setup finds ComfyUI, downloads what the app needs (about 1 GB) and asks for your language model.
3. In the **Library**, import a workflow from ComfyUI and approve its card. Rescan your LoRAs and approve theirs too.
4. On **Plan**, choose the workflow, describe what you want and press **Plan**. Review the jobs, then run them.

A request can be split and shaped with three symbols:

```
for all the images use @InkWash === a knight at a mountain pass --- an empty cabin at night --- 5 dragons
```

`---` splits it into parts planned separately, text before `===` is a rule for every part, and `@` picks a LoRA. The **How it works** page in the app explains the rest.

To update, run the new setup over the old install. Your settings and data are kept.

## Use it from an AI agent

Add the app's MCP endpoint to your agent (the app has to be running):

```json
{
  "mcpServers": {
    "comfy-planner": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

## Known issues

- Windows may show a SmartScreen warning the first time you run the setup, because it is not code-signed yet. Choose **More info**, then **Run anyway**.
- **Start ComfyUI** works with ComfyUI Desktop, comfy-cli and installs that have their own `.venv`. With other installs, such as the portable build, start ComfyUI yourself.
- The first start of a new install takes a few minutes while the app creates its Python environment, and needs an internet connection.

## Problems and feedback

Open an [issue](https://github.com/Ekkusuu/comfy-planner/issues) and attach the logs from `data\logs\` (`planner.log`, and `llm-calls.log` for planning problems), with what you did and what happened. The tray menu has **Open logs folder**.

## Building from source

Needs Python 3.12 with [uv](https://github.com/astral-sh/uv), Node.js and PowerShell.

```powershell
git clone https://github.com/Ekkusuu/comfy-planner
cd comfy-planner
copy config\app.example.toml config\app.toml
.\start.ps1                      # installs dependencies, builds the UI, serves http://127.0.0.1:8765
launcher\build.ps1               # builds Comfy Planner.exe
scripts\build_release.ps1        # builds the setup (needs Inno Setup 6)
uv run pytest -q                 # runs the tests
```

`config/app.example.toml` documents every setting. Releases are built by GitHub Actions when a `v<version>` tag is pushed.

## Credits

Built on [ComfyUI](https://github.com/comfyanonymous/ComfyUI) and [llama.cpp](https://github.com/ggml-org/llama.cpp). Also uses [FastAPI](https://fastapi.tiangolo.com), [Uvicorn](https://www.uvicorn.org), [HTTPX](https://www.python-httpx.org), [Pydantic](https://docs.pydantic.dev), [Pillow](https://python-pillow.org), [tomlkit](https://github.com/python-poetry/tomlkit), the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), [Svelte](https://svelte.dev), [Vite](https://vite.dev), [Geist](https://vercel.com/font), [WebView2](https://developer.microsoft.com/microsoft-edge/webview2/), [uv](https://github.com/astral-sh/uv) and [Inno Setup](https://jrsoftware.org/isinfo.php). The embeddings model is Google's [EmbeddingGemma](https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF); tag data comes from [Danbooru](https://danbooru.donmai.us). LoRA information comes from [Civitai](https://civitai.com).

## License

[MIT](LICENSE)
