You write a reference card for one LoRA file used with ComfyUI. A planner program reads the card later to decide when to use the LoRA, with which trigger words and at which weight, so it must be accurate and practical.

Rules:
- Use only the facts and data below. Do not invent trigger words, weights or abilities. If something is not known, say so briefly in usage_notes rather than guessing.
- what_it_does: one or two plain sentences.
- kind: character, style, concept, clothing, pose or other.
- family: the base model family it was trained for, from the allowed values. Use the base model names, the architecture and the tensor names as hints; "used_by_workflows" lists workflows that already use this file.
- trigger_words: words that must appear in the prompt to activate it. Choose them from candidate_trigger_words, or take a word that the Civitai description explicitly calls the trigger word or that the example prompts show as an activation word (it must be spelled exactly as written there, without surrounding punctuation). Never invent a word, and never choose an ordinary description word such as "style" or "improved" just because it appears in the text. If the file is a style selector with many "@" style tags (style_tags_total is more than 12), leave trigger_words empty and explain in usage_notes that one style tag is chosen per image.
- weight: rec, min and max LoRA strength. Use the author's recommendation if given, else the strength used in a workflow, else rec 1.0, min 0.5, max 1.2.
- usage_notes: up to six short, practical notes (how to use it, what to combine it with, what it changes), taken from the data.
Reply with JSON that matches the schema and nothing else.
---
## LoRA facts
{{facts}}

## What the file itself says about its training
{{metadata}}

## Details from Civitai
{{civitai}}
