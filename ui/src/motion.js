// The sample's easing as a JS function (for Svelte transitions) plus small DOM actions.
function bezier(x1, y1, x2, y2) {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by
  const X = (t) => ((ax * t + bx) * t + cx) * t
  const Y = (t) => ((ay * t + by) * t + cy) * t
  const dX = (t) => (3 * ax * t + 2 * bx) * t + cx
  return (x) => {
    let t = x
    for (let i = 0; i < 8; i++) {
      const err = X(t) - x
      if (Math.abs(err) < 1e-5) break
      const d = dX(t)
      if (Math.abs(d) < 1e-6) break
      t -= err / d
    }
    return Y(Math.min(Math.max(t, 0), 1))
  }
}
export const ease = bezier(0.16, 1, 0.3, 1)

const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches

/** Opacity + small translate/scale; used by popovers, menus, toasts, overlays. */
export function pop(node, { duration = 240, y = -4, scale = 0.97 } = {}) {
  return {
    duration: reduced() ? 0 : duration,
    easing: ease,
    css: (t) => `opacity:${t};transform:translateY(${(1 - t) * y}px) scale(${scale + (1 - scale) * t})`,
  }
}
export function fade(node, { duration = 200 } = {}) {
  return { duration: reduced() ? 0 : duration, easing: ease, css: (t) => `opacity:${t}` }
}
/** Leave animation for a list item: fade and collapse. */
export function leave(node, { duration = 320 } = {}) {
  const h = node.getBoundingClientRect().height
  return {
    duration: reduced() ? 0 : duration,
    easing: ease,
    css: (t) => `opacity:${t};transform:translateY(${(1 - t) * -6}px);height:${h * t}px;margin-top:0;overflow:hidden;`,
  }
}

/** Leave animation for a flex tile: shrink its width to nothing so the neighbours close the gap. */
export function collapse(node, { duration = 380, gap = 8 } = {}) {
  const { flexGrow, flexBasis } = getComputedStyle(node)
  const grow = parseFloat(flexGrow), basis = parseFloat(flexBasis)
  return {
    duration: reduced() ? 0 : duration,
    easing: ease,
    css: (t) => `opacity:${t};transform:scale(${0.92 + 0.08 * t});flex-grow:${grow * t};flex-basis:${basis * t}px;margin-right:${-gap * (1 - t)}px;pointer-events:none;`,
  }
}

/** Run `fn` when a pointer goes down or Escape is pressed outside `node`. */
export function dismissable(node, fn) {
  const down = (e) => {
    if (!node.contains(e.target)) fn(e)
  }
  const key = (e) => {
    if (e.key === 'Escape') {
      e.stopPropagation()
      fn(e)
    }
  }
  addEventListener('pointerdown', down, true)
  node.addEventListener('keydown', key)
  return {
    update(next) { fn = next },
    destroy() {
      removeEventListener('pointerdown', down, true)
      node.removeEventListener('keydown', key)
    },
  }
}

/** Textarea that grows with its content. */
export function autogrow(node) {
  const fit = () => {
    node.style.height = '0px'
    node.style.height = node.scrollHeight + 'px'
  }
  node.addEventListener('input', fit)
  fit()
  requestAnimationFrame(fit)
  let w = node.clientWidth
  const ro = new ResizeObserver(() => {
    if (node.clientWidth !== w) { w = node.clientWidth; fit() }   // refit on width change (window resize, details opening)
  })
  ro.observe(node)
  return { destroy: () => { node.removeEventListener('input', fit); ro.disconnect() } }
}
