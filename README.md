# Comfy Planner

A local Windows app that turns plain-language requests into ComfyUI image jobs. A local language model plans each job: it picks LoRAs and settings for the workflow you choose, writes the prompts, and puts the images in a queue that you can watch in real time. Nothing leaves your computer.

## What you need

- Windows 10 or 11
- An NVIDIA graphics card with enough memory for your image models and your language model. The two take turns on the card, so they do not need to fit at the same time.
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI), with the models your workflows use
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (`llama-server`)
- A language model in GGUF format
- Python 3.12 and [uv](https://github.com/astral-sh/uv)
- Node.js and npm, to build the interface

## Getting started

```powershell
git clone <this repository>
cd comfy-planner
copy config\app.example.toml config\app.toml
.\start.ps1
```

`start.ps1` installs the dependencies, builds the interface the first time, and opens the app at `http://127.0.0.1:8765`.

To run the app in its own window from a Desktop shortcut, build the launcher once with `launcher\build.ps1`.

## First steps in the app

1. **Settings:** set the ComfyUI address and install folder, and the path to `llama-server`.
2. **Settings, Local LLM:** follow the steps there to add your language model. **Open models folder** takes you to the right place.
3. **Library:** import a ComfyUI workflow, then build and approve its card. The card tells the planner what the workflow is good for.
4. **Library:** rescan your LoRAs, which are read from your ComfyUI folders, and build their cards. Import your wildcard lists if you use them. A new install has no workflows, LoRAs or wildcards until you add your own.
5. **Plan:** choose a workflow, describe what you want, and queue the plan.

The **How it works** page in the app explains the request syntax and every step of planning.

Two optional extras improve the results:

- The tag library, used to check tags and to find real tags for vague words: `uv run python scripts/update_taglib.py`
- A small embeddings model, which lets the planner search your wildcard lists and the tag library by meaning, and read from the entries which part of a picture each list is for (any folder layout works, and one list can feed several parts). Set its path under `[embeddings]` in `config/app.toml`.

## How planning works

A request is split into jobs, and each job gets a scene plan before its prompts are written. Your wildcard lists and the tag library supply concrete ideas for the planner to choose from. A set of checks then corrects the prompts for the model family of the workflow. The language model and ComfyUI share the graphics card, so the queue pauses at an image boundary while planning runs and continues afterwards. Several plans can wait in line and run back to back.

## Use it from an AI agent

The app includes an [MCP](https://modelcontextprotocol.io) server, so AI agents can plan and generate images, edit pictures, manage the queue, and change settings and cards. It only accepts connections from your own computer, and the app has to be running.

Add it to your agent's MCP configuration as an HTTP server:

```json
{
  "mcpServers": {
    "comfy-planner": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

The **AI agents (MCP)** section of Settings shows the address and this configuration with the port you set. Agents cannot delete anything, import workflows, download models, or change the settings that decide which programs run or where ComfyUI is.

## Development

```powershell
uv sync
uv run pytest -q
cd ui
npm ci
npm run build
```

The tests need no GPU, models or running services. For live interface work, run `npm run dev` in `ui/` while the backend is running.

Settings live in `config/app.toml`, which is not tracked. `config/app.example.toml` documents every section. Planner prompts are in `config/prompts/` and are read on every call, so an edit needs no restart.

Logs are in `data/logs/`: `planner.log` for planning and the queue, `llm-calls.log` for every language model call in full.

## Content note

Comfy Planner writes prompts for the models, LoRAs and word lists you give it, so what it can describe follows your own setup. When a request asks for it, the app can plan adult content between adults. A content safety filter is always on and has no setting: a request, plan or prompt that matches one of its rules is blocked, with a fixed message on the card. The rules are data in `app/planning/content_rules.toml`, and the comment at the top of that file explains how to add one. If the file is missing or malformed, the app refuses to plan or queue.

## Licence

MIT. See [LICENSE](LICENSE).
