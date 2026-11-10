// Plain helpers for the LoRA catalogue: what a LoRA is called, how it is found, filtered and sorted.
// An entry is one item of /api/cards/loras: { key, state, status, card, user_tags, used, modified, cover, ... }.

export const loraName = (key) => key.replace(/\.safetensors$/i, '')

/** 'approved' | 'draft' | 'none' (no card yet). */
export const statusOf = (e) => (e.status === 'approved' ? 'approved' : e.status === 'draft' ? 'draft' : 'none')
export const STATUS_TEXT = { approved: 'Approved', draft: 'Draft', none: 'No card' }

/** Lower-case text a search looks in: name, trigger words, description, the user's tags and the file's training tags. */
export function searchText(e) {
  const c = e.card
  return [e.key, c?.what_it_does, ...(c?.trigger_words ?? []), ...(e.user_tags ?? []), ...(c?.tags ?? [])].filter(Boolean).join(' ').toLowerCase()
}

/** True if every word typed is somewhere in the text. */
export const matchesQuery = (text, q) => q.toLowerCase().split(/\s+/).filter(Boolean).every((w) => text.includes(w))

/** Distinct values of one facet over some entries, [{ value, count }], most common first. `valuesOf(e)` gives an entry's values. */
export function facet(entries, valuesOf) {
  const counts = new Map()
  for (const e of entries) for (const v of valuesOf(e)) counts.set(v, (counts.get(v) ?? 0) + 1)
  return [...counts].map(([value, count]) => ({ value, count })).sort((a, b) => b.count - a.count || a.value.localeCompare(b.value))
}

export const tagsInUse = (entries) => facet(entries, (e) => e.user_tags ?? []).map((f) => f.value)

/** Filters combine: every group that has a choice must match (any of its chosen values). */
export function passes(e, f) {
  return (
    (!f.kinds.length || f.kinds.includes(e.card?.kind)) &&
    (!f.families.length || f.families.includes(e.card?.family)) &&
    (!f.tags.length || f.tags.some((t) => (e.user_tags ?? []).includes(t))) &&
    (!f.statuses.length || f.statuses.includes(statusOf(e)))
  )
}

export const SORTS = { name: 'Name', newest: 'Newest', used: 'Most used' }

export function sorted(entries, how) {
  const byName = (a, b) => a.key.localeCompare(b.key, undefined, { sensitivity: 'base', numeric: true })
  const by = {
    name: byName,
    newest: (a, b) => b.modified - a.modified || byName(a, b),
    used: (a, b) => b.used - a.used || byName(a, b),
  }
  return [...entries].sort(by[how] ?? byName)
}
