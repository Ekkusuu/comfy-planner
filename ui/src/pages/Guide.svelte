<script>
  import Icon from '../Icon.svelte'

  const steps = [
    { n: 1, icon: 'library', name: 'Library', route: 'library', lead: 'Teach the app what you have.',
      lines: ['Import workflows you built in ComfyUI.', 'The LLM writes a card for each workflow and each LoRA.', 'You check the cards and approve the ones you trust.'] },
    { n: 2, icon: 'plan', name: 'Plan', route: 'plan', lead: 'Decide what to make.',
      lines: ['Choose the workflow, describe what you want and press Plan (Ctrl+Enter).', 'The LLM makes the jobs on your workflow: LoRAs, settings and a different prompt per image, each with a one-line reason.', 'Change anything, lock what you like, Re-plan the rest. Or add a job by hand.'] },
    { n: 3, icon: 'queue', name: 'Queue', route: 'queue', lead: 'ComfyUI draws.',
      lines: ['Jobs run one at a time, in order.', 'Reorder them, cancel them, retry the ones that failed.', 'Progress shows live.'] },
    { n: 4, icon: 'gallery', name: 'Gallery', route: 'gallery', lead: 'Keep it and reuse it.',
      lines: ['Every image keeps its full recipe.', 'Reuse a recipe as a new job, pick a cover for its workflow, or delete what you don’t want.'] },
  ]

  const concepts = [
    ['Job', 'One batch of images from one workflow, with a prompt for each image. A job is what you run.'],
    ['Workflow', 'A recipe you built in ComfyUI: which models to load and how to draw. The app keeps its own copy and never changes yours.'],
    ['Card', 'A short note about a workflow or a LoRA: what it is for, how to prompt it, safe settings. The planner reads cards, not raw workflows. A card is a draft until you approve it.'],
    ['LoRA', 'A small add-on model that gives a style, a character or a pose. It is never part of a workflow: a job can use none, one or several LoRAs, each at its own weight, on any workflow of the same family. Its card lists trigger words and a good weight, and can list negative tags to remove from the negative prompt of any job that uses it.'],
    ['Batch', 'The jobs that came from one request when it gives two or more images. Plan, Queue and Gallery each show it as one group.'],
    ['Recipe', 'Everything used to make one image: prompt, seed, size, steps, LoRAs and workflow. Copy it or turn it back into a job.'],
    ['Lock', 'A mint lock on a setting or prompt means “keep this”. When the LLM plans or re-plans, it won’t change anything you locked.'],
  ]
</script>

<main class="page guide">
  <div class="head"><h1>How it works</h1></div>

  <p class="lede">Comfy Planner turns what you want to see into ComfyUI images. A local LLM does the planning, ComfyUI does the drawing, and you can change anything in between.</p>

  <ol class="flow" aria-label="The four steps">
    {#each steps as s}
      <li class="step">
        <div class="mk"><span class="num">{s.n}</span><span class="line"></span></div>
        <div class="body">
          <h2><Icon name={s.icon} />{s.name}</h2>
          <p class="lead">{s.lead}</p>
          <ul>{#each s.lines as l}<li>{l}</li>{/each}</ul>
          <a class="go" href="#/{s.route}">Open {s.name}</a>
        </div>
      </li>
    {/each}
  </ol>

  <section class="soon" aria-labelledby="soon-h">
    <h2 id="soon-h">Plan with a sentence</h2>
    <p>On the Plan page, pick the workflow you want to use, then type something like “four moody portraits in the rain” in the request box. You always choose the workflow; the LLM never does. It splits the request into jobs on that workflow, picks LoRAs and settings from your approved cards, writes a different prompt for each image, and says why under each choice. Before the prompts it writes a <b>scene plan</b> for each job: the people, clothing, pose, action, expression, camera angle, gaze, place, light and mood, the concrete words you used, and a rating. That is how a vague word like “surprise me” or “very complex” becomes things an image model can draw. Random ideas from your wildcard lists are offered to the scene plan too, for the parts of the picture you left open, and the tags offered for each part are a fresh pick each run, so the same vague request gives different pictures; what you wrote is never replaced. Edit any value and it locks; a re-plan then redoes everything except what you locked. Run sends the finished jobs to the queue.</p>
    <p>The LLM also works out what you want besides the pictures, in whatever words you use. <b>LoRAs:</b> “use <span class="mono">@JADE</span>” and “without <span class="mono">@JADE</span>” are different things; “3 images per style lora, skip mixed styles” makes 3 images for each style LoRA except that one (each image its own idea, each with its LoRA fixed). If a name matches nothing or several LoRAs, or no LoRA fits, the card says so; it never guesses. When your request lists several LoRAs the plan's message and the first card say who was included and who was left out and why (ruled out, card not approved, over the image limit). “You can use the other LoRAs as you wish” lets the planner add LoRAs that fit each picture and say why it passed on the others; “no LoRAs” means none. <b>Content level:</b> a request for nsfw images is planned as adult scenes from the first description on. <b>Variety:</b> “a different angle each time” gives every image its own entry from your camera wildcard lists, never repeated in the batch while others are left.</p>
  </section>

  <section class="stack">
    <h2>Big requests and batches</h2>
    <p>Ask for as many images as you want, up to the limit in Settings (200 by default). “100 unique cyberpunk scenes” gives 100 images, each with its own idea, LoRAs, prompt and settings, as if each had been its own job. The LLM plans them one after another, about 15 to 20 seconds per image, and you can cancel and keep what is done. A request that gives two or more images is a <b>batch</b>: one card on Plan, one row in the Queue and one folder in the Gallery.</p>
    <p>To keep two things apart, put <span class="mono">---</span> between them, inline or on its own line. Each part is planned as a request of its own, so “3 portraits of a queen --- 50 unique cyberpunk scenes” can never be mixed up. Three or more hyphens with a space on both sides count; hyphens inside words or wildcard tokens do not.</p>
    <p>For something that applies to all the parts, write it first and end it with <span class="mono">===</span>: “for all the images use the ink LoRA === a knight --- a cabin --- 5 dragons”. Every job gets it as a demand it has to keep.</p>
    <p>To name a LoRA, type <span class="mono">@</span> and pick it from the list. A LoRA named this way is always used, at its recommended weight, on the images the text applies to; anything else after an <span class="mono">@</span>, like an artist tag, is left alone.</p>
  </section>

  <section class="stack">
    <h2>Stressing something</h2>
    <p>On the Anima workflows the LLM can stress a thing as <span class="mono">(red scarf:1.4)</span>, sparingly: at most three per prompt, never on quality or style tags. Krea-2 and Qwen read those characters as text, so there the LLM stresses with wording and word order instead. A weight you type yourself stays as typed; on Krea-2 and Qwen it is removed and the card says so.</p>
  </section>

  <section class="stack">
    <h2>Real tags</h2>
    <p>For tag workflows (Anima) the LLM is shown real Danbooru tags for each part of the scene plan (tens of them), and afterwards the app checks what it wrote against a local tag list: <span class="mono">1old man</span> becomes <span class="mono">old man</span>, “3 friends” always gets <span class="mono">3girls, multiple girls</span>, a phrase like “rainy hotel room” becomes real tags when all of it can be covered, the concrete words you typed are added back, a very complex request gets at least 35 content tags, and the rating tag is written into the prompt. Changes that matter show under the prompt as “Tag check”. Anything you typed is left as typed. A line under the prompts shows the scene plan in short form and its rating. There is also a cautious check that removes one of two tags that are alternatives for the same thing (such as <span class="mono">blue eyes</span> and <span class="mono">brown eyes</span>) in a picture of one person, using public tag co-occurrence counts you download and build yourself; it is on, catches few cases, and does nothing without that data.</p>
    <p>For a vague word such as “cozy”, “intricate” or “rainy”, the app searches the tag library and your wildcard lists by meaning and offers the real tags that come out. With the optional reranking model installed, the best matches are ordered more carefully. <span class="mono">config/concepts.toml</span> is a small file you can edit, and it holds no tag lists: only the words that set the least rating a request asks for (with the words that raise it a level), the cute appearance words that are left out at the nsfw and explicit ratings, and the ending that marks a light tag.</p>
  </section>

  <section class="stack">
    <h2>Artists and LoRA names</h2>
    <p>An artist or <span class="mono">@style</span> tag is written only when you named one, asked for artists, or a chosen LoRA needs its style tag; otherwise it is removed, and artist wildcard lists are not offered unless you ask. An artist list gives one name by default, or the number you asked for.</p>
    <p>A LoRA is named by you and never describes itself in the prompt: a tag copied word for word from a LoRA card's description is removed, and a mention is written as “the chosen LoRA”, so a name word like <span class="mono">paint</span> cannot reach a prompt.</p>
  </section>

  <div class="pair">
    <section>
      <h2>What the LLM does</h2>
      <p>It reads the approved card of the workflow you chose and your request, then plans the jobs on that workflow. It runs on your PC, with no cloud. The app checks every suggestion before anything is queued, and the LLM never runs anything itself. It is also what writes the draft cards in the Library.</p>
    </section>
    <section>
      <h2>One GPU, two phases</h2>
      <p>Your graphics card can run the LLM or ComfyUI, not both at once. So the app plans first, unloads the LLM, then lets ComfyUI draw. That is why the Queue sometimes says it is paused for planning, and why the Plan page shows “Loading the LLM” first.</p>
    </section>
  </div>

  <section class="stack">
    <h2>Words you will see</h2>
    <dl class="terms">
      {#each concepts as [term, text]}
        <div><dt>{term}</dt><dd>{text}</dd></div>
      {/each}
    </dl>
  </section>

  <p class="foot">Hover the <span class="i-hint"><Icon name="info" /></span> next to a setting or button anywhere in the app for a short explanation.</p>
</main>

<style>
  .guide { max-width: 1000px; gap: 36px; }
  .lede { font-size: 15px; line-height: 1.6; color: var(--text-soft); max-width: 62ch; margin-top: -14px; }
  h2 { font-size: 15px; font-weight: 600; letter-spacing: -0.005em; color: var(--text); }
  p { color: var(--muted); font-size: 13.5px; line-height: 1.6; max-width: 66ch; }

  .flow { list-style: none; margin: 0; padding: 0; display: grid; gap: 14px; grid-template-columns: minmax(0, 1fr); }
  .step { display: grid; gap: 12px; align-content: start; grid-template-rows: auto 1fr; }
  .mk { display: flex; align-items: center; gap: 10px; }
  .num { width: 24px; height: 24px; border-radius: 50%; background: var(--mint-dim); border: 1px solid var(--mint-line); color: var(--mint); display: grid; place-items: center; font-size: 12px; font-weight: 600; flex: none; }
  .line { display: none; flex: 1; height: 1px; background: var(--line2); }
  .body { background: var(--surface); border: 1px solid var(--line); border-radius: var(--r-2xl); padding: 16px 18px 14px; display: grid; gap: 8px; align-content: start; }
  .body h2 { display: flex; align-items: center; gap: 8px; }
  .body h2 :global(svg.i) { color: var(--muted); }
  .lead { color: var(--text); font-size: 13.5px; }
  .body ul { margin: 0; padding: 0; list-style: none; display: grid; gap: 7px; color: var(--muted); font-size: 13px; line-height: 1.5; }
  .body li { position: relative; padding-left: 14px; }
  .body li::before { content: ''; position: absolute; left: 1px; top: 8px; width: 4px; height: 4px; border-radius: 50%; background: var(--faint); }
  .go { margin-top: 4px; width: fit-content; color: var(--text-soft); font-size: 13px; border-bottom: 1px solid var(--line2); transition: color 0.2s var(--ease), border-color 0.2s var(--ease); }
  .go:hover { color: var(--text); border-color: var(--mint-line); }

  .soon { padding: 18px 20px; border: 1px solid var(--mint-line); background: var(--mint-dim); border-radius: var(--r-2xl); display: grid; gap: 6px; }
  .soon p { color: var(--text-soft); max-width: 72ch; }

  .pair { display: grid; gap: 28px; grid-template-columns: minmax(0, 1fr); }
  .pair section { display: grid; gap: 6px; align-content: start; }

  .terms { margin: 0; display: grid; grid-template-columns: minmax(0, 1fr); border-top: 1px solid var(--line); }
  .terms > div { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 16px; padding: 12px 0; border-bottom: 1px solid var(--line); }
  .terms dt { font-weight: 500; }
  .terms dd { margin: 0; color: var(--muted); font-size: 13.5px; line-height: 1.55; max-width: 70ch; }

  .foot { font-size: 13px; }
  .i-hint { display: inline-grid; vertical-align: -3px; color: var(--faint); }

  @media (min-width: 1000px) { .flow { grid-template-columns: repeat(2, minmax(0, 1fr)); } .pair { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (min-width: 1360px) {
    .guide { max-width: 1180px; }
    .flow { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .line { display: block; }
    .step:last-child .line { display: none; }
  }
  @media (max-width: 600px) { .terms > div { grid-template-columns: 1fr; gap: 2px; } }
</style>
