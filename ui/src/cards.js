// Helpers for the card editors: build state, diff between two versions of a card, slot rows.

const STAGES = ['queued', 'waiting', 'loading', 'reading', 'writing']
export const isBuilding = (b) => !!b && STAGES.includes(b.stage)
export const clone = (o) => JSON.parse(JSON.stringify(o))
export const same = (a, b) => JSON.stringify(a) === JSON.stringify(b)

/** Text shown next to a running build ("Loading the LLM…"). */
export const buildText = (b) => (b ? b.message.replace(/\.$/, '') + '…' : '')

// ---------- diff ----------

// Fields that code fills or that are bookkeeping: not worth showing in a diff of what the LLM wrote.
const SKIP = new Set(['built_at', 'built_with', 'sha256', 'file', 'models', 'input_images', 'default_loras', 'notes_full', 'civitai', 'style_tags', 'notice', 'metadata_only', 'needs_attention'])
const LABELS = {
  purpose: 'Purpose', best_for: 'Best for', family: 'Family', 'prompt_style.kind': 'Prompt style', 'prompt_style.quality_prefix': 'Quality prefix',
  'prompt_style.rules': 'Prompt rules', 'settings.cfg': 'CFG', 'settings.steps': 'Steps', 'settings.samplers': 'Samplers', 'settings.schedulers': 'Schedulers',
  'settings.sizes': 'Sizes', 'settings.pixel_budget': 'Pixel budget', 'settings.hires.steps': 'Hires steps', 'settings.hires.denoise': 'Hires denoise',
  warnings: 'Warnings', examples: 'Examples', use_full_notes: 'Use full notes', default_negative: 'Default negative', what_it_does: 'What it does', kind: 'Kind',
  trigger_words: 'Trigger words', remove_from_negative: 'Remove from negative', sets_look: 'The LoRA sets the look', tags: 'Tags', weight: 'Weight', usage_notes: 'Usage notes',
}
const isRange = (v) => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).every((k) => ['rec', 'min', 'max'].includes(k))

function show(v) {
  if (v == null || v === '') return ''
  if (typeof v === 'boolean') return v ? 'Yes' : 'No'
  if (Array.isArray(v)) return v.map((x) => (x && typeof x === 'object' ? `${x.w}×${x.h}${x.label ? ' ' + x.label : ''}` : String(x))).join('\n')
  if (isRange(v)) return [['rec', v.rec], ['min', v.min], ['max', v.max]].filter(([, n]) => n != null).map(([k, n]) => `${k} ${n}`).join(' · ')
  return String(v)
}

function flatten(card, prefix = '', out = []) {
  for (const [k, v] of Object.entries(card ?? {})) {
    const path = prefix ? `${prefix}.${k}` : k
    if (SKIP.has(k)) continue
    if (v && typeof v === 'object' && !Array.isArray(v) && !isRange(v)) flatten(v, path, out)
    else out.push({ path, label: LABELS[path] ?? path, text: show(v) })
  }
  return out
}

/** Fields whose text differs between two versions of a card: [{ label, before, after }]. */
export function diffCards(before, after) {
  const old = new Map(flatten(before).map((f) => [f.path, f]))
  const out = []
  for (const f of flatten(after)) {
    const o = old.get(f.path)
    if ((o?.text ?? '') !== f.text) out.push({ label: f.label, before: o?.text ?? '', after: f.text })
  }
  return out
}

// ---------- workflow slots (read-only table) ----------

const shown = (v) => (v == null || v === '' ? '' : typeof v === 'string' ? (v.length > 90 ? v.slice(0, 89) + '…' : v) : JSON.stringify(v))
const where = (s) => (s ? `${s.node} · ${s.input ?? 'output ' + s.output}` : null)

/** Rows [label, node · input, default value] for the slot map of a workflow. */
export function slotRows(wf) {
  const s = wf.slots, d = s.defaults ?? {}
  const rows = []
  const add = (label, slot, def) => slot && rows.push([label, where(slot), shown(def)])
  add('Positive prompt', s.positive, d.positive)
  add('Negative prompt', s.negative, d.negative)
  add('Seed', s.seed, d.seed)
  add('Steps', s.steps, d.steps)
  add('CFG', s.cfg, d.cfg)
  add('Sampler', s.sampler, d.sampler)
  add('Scheduler', s.scheduler, d.scheduler)
  add('Denoise', s.denoise, d.denoise)
  add('Width', s.width, d.width)
  add('Height', s.height, d.height)
  add('Model loader', s.model, null)
  if (s.hires) for (const k of ['steps', 'cfg', 'sampler', 'scheduler', 'denoise']) add(`Hires ${k}`, s.hires[k], d.hires?.[k])
  for (const sw of s.switches ?? []) rows.push([`Switch · ${sw.title}`, where(sw), `${sw.role}, default ${sw.default}`])
  for (const im of s.input_images ?? []) rows.push([`Input image · ${im.label}`, where(im), null])
  for (const o of s.outputs ?? []) rows.push(['Output', where(o), null])
  if (s.lora_loaders?.length) rows.push(['LoRA loaders', s.lora_loaders.join(', '), shown((d.loras ?? []).map((l) => l.name).join(', '))])
  return rows
}
