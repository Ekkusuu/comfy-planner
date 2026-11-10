# Product

Comfy Planner is a local web app on one Windows PC. A local LLM turns a plain-language request into ComfyUI image jobs: you choose the workflow, then it picks LoRAs and settings for it, writes a distinct prompt per image, and says why. Code validates, queues and runs the jobs.

## User

One person, the owner of the machine. Technically fluent (runs ComfyUI, reads JSON, edits config), uses it in long creative sessions, usually at a desk in the evening. Wants the machine to decide by default and to override any field in one click.

## Surfaces

All are Operate mode (a working tool): Plan (request box and job cards), Queue, Gallery and history, Library (workflow and LoRA cards), Settings.

## Brand commitments

- Clean, professional, production-grade feel; smooth, purposeful animations.
- Should sit alongside **Vercel** and **Raycast**: monochrome, precise, calm, the product's own content (the images) as the main colour.
- **Dark** interface only.
- **Mint** as the accent colour.
- The user rejected an expressive "pencil and ink" concept as unattractive; convention executed with high craft is the goal.

## Content rules

- No content filtering in the UI or prompts beyond one fixed, always-on safety filter. Its rules are data in `app/planning/content_rules.toml`; a request or prompt that matches a rule is blocked.
- Copy is plain, sentence case, names actions by what they do (Plan, Run, Re-plan, Lock).
