// Shared live state: routing, ComfyUI health, jobs + progress from the SSE stream, toasts.
import { api } from './api.js'

// ---------- routing (hash) ----------
export const route = $state({ name: 'plan', arg: null, query: {} })

const titles = { plan: 'Plan', queue: 'Queue', gallery: 'Gallery', library: 'Library', how: 'How it works', settings: 'Settings' }
function parseHash() {
  const [path, qs = ''] = location.hash.replace(/^#\/?/, '').split('?')
  const [name, arg] = path.split('/')
  const next = Object.hasOwn(titles, name) ? name : 'plan'
  if (next !== route.name) scrollTo(0, 0) // a new page starts at the top, not where the last one was scrolled
  route.name = next
  document.title = `${titles[next]} · Comfy Planner`
  route.arg = arg || null
  route.query = Object.fromEntries(new URLSearchParams(qs))
}
parseHash()
addEventListener('hashchange', parseHash)
export const go = (name, query) => {
  const qs = query ? '?' + new URLSearchParams(query) : ''
  location.hash = `#/${name}${qs}`
}

// ---------- toasts ----------
export const toasts = $state([])
let toastId = 0
export function toast(text, opt = {}) {
  const t = { id: ++toastId, text, kind: opt.kind || 'ok', action: opt.action }
  toasts.push(t)
  setTimeout(() => dismiss(t.id), opt.ms || (opt.action ? 6000 : 3200))
}
export const toastError = (e) => toast(e?.message || String(e), { kind: 'err', ms: 6000 })
export function dismiss(id) {
  const i = toasts.findIndex((t) => t.id === id)
  if (i >= 0) toasts.splice(i, 1)
}

// ---------- live state ----------
export const live = $state({
  health: null, // /api/health or null while unknown
  jobs: [], // /api/jobs (with image_counts)
  images: [], // finished images, newest first
  workflows: [],
  options: { available: false, samplers: [], schedulers: [], loras: [] },
  config: { max_images_per_job: 16, max_images_per_request: 200, hires_default: true, card_families: [], lora_kinds: [] },
  progress: {}, // job_id -> { idx, frac, node }
  preview: {}, // job_id -> number of the newest step preview of the image being generated (the picture is fetched by number)
  plans: {}, // plan id -> /api/plans/{id} record (status queued|waiting|loading|planning|done|failed|cancelled; queue_position 1 = next up in the line)
  cardBuilds: {}, // 'workflow:<id>' | 'lora:<file>' -> latest build state { stage, message, ... }
  workflowCards: [], // /api/cards/workflows (status per workflow)
  loras: [], // /api/cards/loras (every LoRA file / card)
  wildcardsStamp: 0, // goes up when a wildcard list is deleted, so the Wildcards section loads its lists again
  ready: false,
})

export const comfyUp = () => !!live.health?.comfy?.reachable

export async function refreshHealth() {
  try {
    live.health = await api.health()
  } catch {
    live.health = null
  }
}
export async function refreshJobs() {
  try {
    live.jobs = await api.jobs()
  } catch {}
}
export async function refreshPlans() {
  try {
    for (const p of await api.plans(5)) live.plans[p.id] = p
  } catch {}
}
export async function refreshImages() {
  try {
    live.images = await api.images(300)
  } catch {}
}
export async function refreshWorkflows() {
  try {
    live.workflows = await api.workflows()
  } catch {}
}
/** Check every workflow against the running ComfyUI. Returns the error (ComfyUI down) or null; the list then falls back to the last check. */
export async function recheckWorkflows() {
  try {
    live.workflows = await api.recheckWorkflows()
    return null
  } catch (e) {
    await refreshWorkflows()
    return e
  }
}
export async function refreshWorkflowCards() {
  try {
    live.workflowCards = await api.cards('workflows')
  } catch {}
}
export async function refreshLoras(rescan = false) {
  try {
    live.loras = rescan ? await api.rescanLoras() : await api.cards('loras')
  } catch (e) {
    if (rescan) throw e
  }
}
/** Add and remove the user's tags on LoRAs; the list on screen follows at once. Throws the server's message. */
export async function changeLoraTags(keys, add = [], remove = []) {
  const { tags } = await api.loraTags(keys, add, remove)
  for (const entry of live.loras) if (entry.key in tags) entry.user_tags = tags[entry.key]
}
/** After a LoRA, workflow or wildcard list was deleted (here or in another window): load what it touched again. */
export function afterDelete(kind) {
  if (kind === 'lora') refreshLoras()
  else if (kind === 'workflow') {
    refreshWorkflows()
    refreshWorkflowCards()
    refreshJobs() // its jobs no longer name it
  } else if (kind === 'wildcard') live.wildcardsStamp++
}
export async function refreshCardBuilds() {
  try {
    for (const b of await api.cardBuilds()) live.cardBuilds[`${b.kind}:${b.key}`] = b
  } catch {}
}
export async function refreshConfig() {
  try {
    live.config = await api.config()
  } catch {}
}
export async function refreshOptions() {
  try {
    live.options = await api.options()
  } catch {}
}

const debounce = (fn, ms) => {
  let t
  return () => {
    clearTimeout(t)
    t = setTimeout(fn, ms)
  }
}
const softJobs = debounce(refreshJobs, 120)
const softImages = debounce(refreshImages, 200)
const softHealth = debounce(refreshHealth, 300)
const softLoras = debounce(refreshLoras, 300)
const softCards = debounce(() => {
  refreshWorkflowCards()
  refreshLoras()
}, 200)

function onEvent(ev) {
  switch (ev.type) {
    case 'plan':
      live.plans[ev.plan.id] = ev.plan
      softJobs()
      break
    case 'queue':
      if (live.health) live.health.queue = ev.queue
      softHealth()
      break
    case 'job':
      if (ev.status !== 'running') delete live.progress[ev.job_id]
      if (ev.status !== 'done') delete live.preview[ev.job_id] // a finished job keeps its last frame until its thumbnail is there
      softJobs()
      if (ev.status === 'done') softImages()
      break
    case 'image':
      if (ev.status === 'running') live.progress[ev.job_id] = { idx: ev.idx, frac: 0, node: null }
      if (ev.status !== 'done') delete live.preview[ev.job_id] // a finished image keeps its last frame until its thumbnail is there
      softJobs()
      if (ev.status === 'done') softImages()
      break
    case 'lora_cover':
      softLoras() // a LoRA's cover was fetched in the background
      break
    case 'library':
      afterDelete(ev.kind) // something was deleted, perhaps from another window
      break
    case 'card': {
      live.cardBuilds[`${ev.kind}:${ev.key}`] = ev
      if (ev.stage === 'done' || ev.stage === 'failed') softCards()
      break
    }
    case 'progress': {
      const p = live.progress[ev.job_id] || (live.progress[ev.job_id] = { idx: ev.idx, frac: 0, node: null })
      p.idx = ev.idx
      p.frac = ev.max ? ev.value / ev.max : 0
      p.node = ev.node
      break
    }
    case 'preview':
      live.preview[ev.job_id] = ev.number
      break
    case 'executing': {
      const p = live.progress[ev.job_id]
      if (p) p.node = ev.node
      break
    }
  }
}

let source
export function connect() {
  if (source) return
  source = new EventSource('/api/events')
  source.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data))
    } catch {}
  }
  // EventSource reconnects by itself; re-sync everything once it does.
  source.onopen = () => {
    refreshJobs()
    refreshImages()
    refreshHealth()
    refreshCardBuilds()
    refreshPlans()
  }
}

export async function boot() {
  connect()
  await Promise.all([refreshHealth(), refreshJobs(), refreshImages(), refreshWorkflows(), refreshOptions(), refreshConfig(), refreshCardBuilds(), refreshPlans(), refreshWorkflowCards()])
  live.ready = true
  refreshLoras() // covers on the job cards; not waited for, the first scan can take a while
  setInterval(refreshHealth, 6000)
  // options need ComfyUI; pick them up once it comes back
  let wasUp = comfyUp()
  setInterval(() => {
    if (comfyUp() && (!wasUp || !live.options.available)) refreshOptions()
    wasUp = comfyUp()
  }, 3000)
}

// ---------- derived helpers ----------
export const runningJobs = () => live.jobs.filter((j) => j.status === 'running')
export const queuedJobs = () => live.jobs.filter((j) => j.status === 'queued')
export const activeCount = () => runningJobs().length + queuedJobs().length

/** Drop deleted images from the gallery now; workflow covers may have changed too. */
export function removeImages(ids) {
  const gone = new Set(ids)
  live.images = live.images.filter((i) => !gone.has(i.id))
  refreshWorkflows()
}

/** First finished image per job, for thumbnails. */
export function thumbFor(jobId) {
  let best = null
  for (const im of live.images) if (im.job_id === jobId && (!best || im.idx < best.idx)) best = im
  return best
}

export const clip = (s, n) => (s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s)
export const jobTitle = (job) => {
  if (job.description) return job.description
  const p = job.fields?.prompts?.value?.[0]
  return p ? clip(p, 56) : 'Untitled job'
}
export const workflowTitle = (id) => (id ? live.workflows.find((w) => w.id === id)?.title || id : 'No workflow yet')

export const isActivePlan = (p) => !!p && p.status !== 'done' && p.status !== 'failed' && p.status !== 'cancelled'
/** Every planning run that is still working or waiting in the line, in the order submitted (the first is the one being worked on). */
export const activePlans = () => Object.values(live.plans).filter(isActivePlan).sort((a, b) => a.id - b.id)
export const blockingIssues = (job) => (job.issues ?? []).filter((i) => i.severity === 'blocking')

// ---------- batches: the jobs one request made ----------
/** Group a list of jobs for display. Jobs from one request that gave several jobs become one {batch: true, id, key, request, jobs};
 *  every other job is {job, key}. The group sits where its first job is. */
export function groupJobs(jobs) {
  const sizes = new Map()
  for (const j of jobs) if (j.batch_id != null) sizes.set(j.batch_id, (sizes.get(j.batch_id) ?? 0) + 1)
  const out = []
  const made = new Map()
  for (const j of jobs) {
    if (j.batch_id == null || sizes.get(j.batch_id) < 2) {
      out.push({ job: j, key: `j${j.id}` })
      continue
    }
    let b = made.get(j.batch_id)
    if (!b) {
      b = { batch: true, id: j.batch_id, key: `b${j.batch_id}`, request: j.batch_request || j.meta?.request || '', jobs: [] }
      made.set(j.batch_id, b)
      out.push(b)
    }
    b.jobs.push(j)
  }
  return out
}
/** The rows of the Queue page: jobs that were run (or wait to), grouped into batches; jobs you cleared are left out. */
export function queueEntries() {
  const shown = ['queued', 'running', 'done', 'failed', 'cancelled']
  return groupJobs(live.jobs.filter((j) => shown.includes(j.status) && !j.cleared_at))
}
/** running | queued | done | problems: the section of the Queue page a row belongs to. A batch is running if any job runs,
 *  queued if any waits, done when all are done, otherwise problems (failed or cancelled). */
export function entryState(e) {
  const js = e.batch ? e.jobs : [e.job]
  if (js.some((j) => j.status === 'running')) return 'running'
  if (js.some((j) => j.status === 'queued')) return 'queued'
  return js.every((j) => j.status === 'done') ? 'done' : 'problems'
}
/** Images of a batch (or one job) that are made, and in total. */
export const imageTotals = (jobs) => ({
  done: jobs.reduce((n, j) => n + (j.image_counts?.done ?? 0), 0),
  total: jobs.reduce((n, j) => n + j.count, 0),
})
/** A batch's cover: the first finished image of its first job that has one. */
export function batchThumb(jobs) {
  for (const j of jobs) {
    const t = thumbFor(j.id)
    if (t) return t
  }
  return null
}

// A "pending prefill" from the gallery ("Use as new job"), consumed by the Plan page.
export const prefill = $state({ recipe: null })

// ---------- start ComfyUI ----------
export const ui = $state({ starting: false })
export async function startComfy() {
  ui.starting = true
  try {
    const r = await api.startComfy()
    // `comfy launch --background` returns before the server answers; wait for it.
    for (let i = 0; i < 40 && !comfyUp(); i++) {
      await refreshHealth()
      if (!comfyUp()) await new Promise((res) => setTimeout(res, 1500))
    }
    if (comfyUp()) toast('ComfyUI is ready')
    else toast(r.exit_code ? `Could not start ComfyUI: ${r.output.trim().split('\n').pop()}` : 'ComfyUI is starting, give it a moment', { kind: 'err' })
  } catch (e) {
    toastError(e)
  } finally {
    ui.starting = false
  }
}

// Local preview URLs for uploaded input images (name on ComfyUI -> object URL), this session only.
export const previews = $state({})

// ---------- jobs + workflows helpers ----------
export function upsertJob(job) {
  const { images, ...rest } = job
  const i = live.jobs.findIndex((j) => j.id === rest.id)
  if (i >= 0) live.jobs[i] = { ...live.jobs[i], ...rest }
  else live.jobs.push(rest)
}
export const removeJob = (id) => {
  const i = live.jobs.findIndex((j) => j.id === id)
  if (i >= 0) live.jobs.splice(i, 1)
}

const wfCache = new Map()
/** Full workflow (with slots + defaults), cached per session. */
export function getWorkflow(id, fresh = false) {
  if (fresh || !wfCache.has(id)) wfCache.set(id, api.workflow(id))
  return wfCache.get(id)
}
