// Thin fetch wrapper. Errors carry the backend's `detail` message so they can be shown as is.
async function req(method, path, body, raw) {
  const opt = { method, headers: {} }
  if (raw !== undefined) opt.body = raw
  else if (body !== undefined) {
    opt.body = JSON.stringify(body)
    opt.headers['Content-Type'] = 'application/json'
  }
  let res
  try {
    res = await fetch(path, opt)
  } catch {
    throw new Error("Can't reach the Comfy Planner app. Is it running?")
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`
    let fields
    try {
      const j = await res.json()
      if (typeof j.detail === 'string') detail = j.detail
      else if (Array.isArray(j.detail)) detail = j.detail.map((d) => d.msg).join('; ')
      fields = j.errors
    } catch {}
    throw Object.assign(new Error(detail), { fields })
  }
  return res.status === 204 ? null : res.json()
}

const deletePath = (kind, key) =>
  kind === 'lora' ? `/api/loras/${encodeURI(key)}` : kind === 'workflow' ? `/api/workflows/${encodeURIComponent(key)}` : `/api/wildcards/${encodeURI(key)}`

export const api = {
  health: () => req('GET', '/api/health'),
  config: () => req('GET', '/api/config'),
  saveConfig: (changes) => req('PUT', '/api/config', changes),
  openModelsFolder: () => req('POST', '/api/system/open-models-folder'),
  // first-run setup
  setupStatus: () => req('GET', '/api/setup/status'),
  setupComfy: () => req('GET', '/api/setup/comfy'),
  setupSetComfy: (change) => req('POST', '/api/setup/comfy', change),
  setupInstall: () => req('POST', '/api/setup/install'),
  setupCancel: () => req('POST', '/api/setup/install/cancel'),
  setupModel: (name, path) => req('POST', '/api/setup/llm-model', { name, path }),
  updateCheck: () => req('GET', '/api/update-check'),
  options: () => req('GET', '/api/comfy/options'),
  startComfy: () => req('POST', '/api/comfy/start'),
  savedWorkflows: () => req('GET', '/api/comfy/workflows'),
  upload: (file) => req('POST', `/api/comfy/upload?filename=${encodeURIComponent(file.name)}`, undefined, file),
  workflows: () => req('GET', '/api/workflows'),
  recheckWorkflows: () => req('POST', '/api/workflows/recheck'),
  setCover: (id, imageId) => req('PUT', `/api/workflows/${id}/cover`, { image_id: imageId }),
  workflow: (id) => req('GET', `/api/workflows/${id}`),
  importWorkflow: (path) => req('POST', '/api/workflows/import', { path }),
  jobs: () => req('GET', '/api/jobs'),
  job: (id) => req('GET', `/api/jobs/${id}`),
  createJob: (body) => req('POST', '/api/jobs', body),
  patchJob: (id, patch) => req('PATCH', `/api/jobs/${id}`, patch),
  deleteJob: (id) => req('DELETE', `/api/jobs/${id}`),
  clearJob: (id) => req('POST', `/api/jobs/${id}/clear`),
  plan: (request, workflowId, inputImages = []) => req('POST', '/api/plan', { request, workflow_id: workflowId, input_images: inputImages }),
  plans: (limit = 5) => req('GET', `/api/plans?limit=${limit}`),
  getPlan: (id) => req('GET', `/api/plans/${id}`),
  replan: (jobIds) => req('POST', '/api/plan/replan', jobIds ? { job_ids: jobIds } : {}),
  replanJob: (id) => req('POST', `/api/jobs/${id}/replan`),
  setLocks: (id, body) => req('POST', `/api/jobs/${id}/locks`, body),
  queueJob: (id) => req('POST', `/api/jobs/${id}/queue`),
  cancelJob: (id) => req('POST', `/api/jobs/${id}/cancel`),
  retryJob: (id, mode = 'failed') => req('POST', `/api/jobs/${id}/retry`, { mode }),
  moveJob: (id, delta) => req('POST', `/api/jobs/${id}/move`, { delta }),
  cancelPlan: (id) => req('POST', `/api/plans/${id}/cancel`),
  queueBatch: (id) => req('POST', `/api/batches/${id}/queue`),
  cancelBatch: (id) => req('POST', `/api/batches/${id}/cancel`),
  clearBatch: (id) => req('POST', `/api/batches/${id}/clear`),
  deleteBatch: (id) => req('DELETE', `/api/batches/${id}`),
  gallery: (limit = 200, offset = 0) => req('GET', `/api/gallery?limit=${limit}&offset=${offset}`),
  galleryGroup: (key) => req('GET', `/api/gallery/groups/${key}`),
  deleteGalleryGroup: (key) => req('DELETE', `/api/gallery/groups/${key}`),
  setOrder: (ids) => req('PUT', '/api/queue/order', { job_ids: ids }),
  images: (limit = 200, offset = 0, jobId) =>
    req('GET', `/api/images?limit=${limit}&offset=${offset}${jobId != null ? `&job_id=${jobId}` : ''}`),
  imageUrl: (id) => `/api/images/${id}/file`,
  thumbUrl: (id, size = 512) => `/api/images/${id}/thumb?size=${size}`,
  previewUrl: (jobId, number) => `/api/jobs/${jobId}/preview?n=${number}`, // newest step preview of a running job; n only keeps the browser from reusing an older frame
  deleteImage: (id) => req('DELETE', `/api/images/${id}`),
  // cards: kind is 'workflows' or 'loras'; key is the workflow id or the LoRA file name
  cards: (kind) => req('GET', `/api/cards/${kind}`),
  card: (kind, key) => req('GET', `/api/cards/${kind}/${encodeURI(key)}`),
  saveCard: (kind, key, card) => req('PUT', `/api/cards/${kind}/${encodeURI(key)}`, card),
  buildCard: (kind, key) => req('POST', `/api/cards/${kind}/${encodeURI(key)}/build`),
  approveCard: (kind, key) => req('POST', `/api/cards/${kind}/${encodeURI(key)}/approve`),
  acceptCard: (kind, key) => req('POST', `/api/cards/${kind}/${encodeURI(key)}/accept`),
  rejectCard: (kind, key) => req('POST', `/api/cards/${kind}/${encodeURI(key)}/reject`),
  rescanLoras: () => req('POST', '/api/cards/loras/rescan'),
  // the user's own LoRA tags and covers (kept apart from the card, so a card rebuild can't wipe them)
  loraTags: (keys, add = [], remove = []) => req('POST', '/api/loras/tags', { keys, add, remove }),
  chooseLoraCover: (key, imageId) => req('PUT', `/api/loras/${encodeURI(key)}/cover`, { image_id: imageId }),
  uploadLoraCover: (key, file) => req('POST', `/api/loras/${encodeURI(key)}/cover/upload`, undefined, file),
  resetLoraCover: (key) => req('DELETE', `/api/loras/${encodeURI(key)}/cover`),
  cardBuilds: () => req('GET', '/api/cards/builds'),
  // wildcards: name is a list name like background/background-location; folder is its first part
  wildcards: () => req('GET', '/api/wildcards'),
  wildcard: (name) => req('GET', `/api/wildcards/${encodeURI(name)}`),
  importWildcards: (path) => req('POST', '/api/wildcards/import', { path }),
  patchWildcard: (name, change) => req('PATCH', `/api/wildcards/${encodeURI(name)}`, change),
  // deleting (files go to the Recycle Bin): kind is 'lora' | 'workflow' | 'wildcard'. The plan only describes; deleteItem does it.
  deletePlan: (kind, key) => req('GET', `${deletePath(kind, key)}/delete-plan`),
  deleteItem: (kind, key, choices = {}) => req('DELETE', kind === 'lora' ? `${deletePath(kind, key)}/file` : deletePath(kind, key), choices),
  patchWildcardFolder: (folder, change) => req('PATCH', `/api/wildcard-folders/${encodeURIComponent(folder)}`, change),
}
