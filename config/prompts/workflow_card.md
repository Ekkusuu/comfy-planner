You write a reference card for one ComfyUI image workflow. A planner program reads the card later to choose workflows and to write prompts and settings for them, so it must be accurate, short and practical.

Rules:
- Use the facts and the author's notes below. Where they say nothing, choose a modest value that fits the model family and the workflow's current settings. Do not invent features.
- purpose: one or two plain sentences on what the workflow makes and how it works.
- best_for: a short phrase on the kinds of requests it suits.
- family: the model family, from the allowed values. Use the model file names as a hint. Use "other" only if nothing fits.
- prompt_style.kind: "tags" for comma-separated tag prompts, "sentences" for natural-language prompts, "mixed" for both.
- prompt_style.quality_prefix: the exact quality or score tags every prompt should start with, copied from the notes or the default prompt. Only general quality tags (masterpiece, best quality, score_9, absurdres and the like): leave out anything about who or what is in the picture, its framing or its look (1girl, solo, upper body, fair skin, high contrast). Empty string if there are none.
- prompt_style.rules: short instructions for writing prompts for this workflow (order of tags, what to avoid, length), taken from the notes.
- settings.cfg and settings.steps: rec is the value the notes recommend (else the current value); min and max are the range in which the workflow still works well.
- settings.samplers and settings.schedulers: only the ones the notes recommend, besides the current ones.
- settings.sizes: the image sizes (width, height, a short label) the notes recommend, or sizes that suit the model: portrait, square and landscape shapes, every side a multiple of 16, inside the pixel budget. Never a size the notes warn against. settings.pixel_budget: the smallest and largest total pixel count (width times height) that works.
- settings.hires: include it only when the facts say has_hires is true; steps and denoise as ranges for the second pass.
- warnings: hard limits and pitfalls from the notes (for example "CFG must stay at 1"). Leave the list empty if there are none.
- examples: 2 or 3 complete prompts in this workflow's prompt style, ready to use, on different subjects. Base them on the notes and the default prompt.
- use_full_notes: true only if the notes are long and hold details (tag lists, step-by-step rules) that this card cannot carry.
Reply with JSON that matches the schema and nothing else.
---
## Workflow facts
{{facts}}

## Notes written by the workflow author
{{notes}}

## Default prompts saved in the workflow
{{defaults}}
