// @ mentions of LoRAs in the request. The same rules as app/planning/mentions.py: `@` + a known LoRA file name without
// its extension, not case sensitive, longest name first, ending where a name can't go on. Anything else after an @ is an artist or style tag.

export const stem = (key) => key.replace(/\\/g, '/').split('/').pop().replace(/\.(safetensors|ckpt|pt)$/i, '')

/** The name as a person reads it: "style-a-anima-base-epoch-05" is "style a anima base epoch 05". */
export const pretty = (key) => stem(key).replace(/[-_]+/g, ' ')

const escape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** [{ start, end, key }] of every mention in the text, in the order written. `keys` are LoRA file names. */
export function scan(text, keys) {
  const byStem = new Map()
  for (const key of keys) {
    const name = stem(key).replace(/\s+/g, ' ').toLowerCase()
    if (name && !byStem.has(name)) byStem.set(name, key)
  }
  if (!byStem.size) return []
  const names = [...byStem.keys()].sort((a, b) => b.length - a.length || (a < b ? -1 : 1)).map((n) => n.split(' ').map(escape).join('\\s+'))
  const found = new RegExp(`(?<![\\p{L}\\p{N}_@])@(${names.join('|')})(?![\\p{L}\\p{N}_-]|\\.[\\p{L}\\p{N}_])`, 'giu')
  return [...(text ?? '').matchAll(found)].map((m) => ({ start: m.index, end: m.index + m[0].length, key: byStem.get(m[1].replace(/\s+/g, ' ').toLowerCase()) }))
}

/** The files the text names, each once. */
export const mentioned = (text, keys) => [...new Set(scan(text, keys).map((m) => m.key))]

/** The text without the mentions of one LoRA (and the space they sat in). */
export function withoutMention(text, key, keys) {
  for (const m of scan(text, keys).filter((m) => m.key === key).reverse()) {
    const after = text[m.end] === ' ' ? 1 : 0
    const before = !after && m.start > 0 && text[m.start - 1] === ' ' ? 1 : 0
    text = text.slice(0, m.start - before) + text.slice(m.end + after)
  }
  return text
}

/** The word being typed after an @ just before the caret: { at, query } or null. The @ must start a word. */
export function typing(text, caret) {
  const m = /(?<![\p{L}\p{N}_@])@(\S[^@\n]{0,40}|)$/u.exec(text.slice(0, caret))
  return m ? { at: m.index, query: m[1] } : null
}
