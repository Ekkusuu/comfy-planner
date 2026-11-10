// Svelte action: turn the vertical mouse wheel into smooth horizontal scrolling on a row.
// While the pointer is over a row that can scroll, the wheel only ever moves the row, also at its ends:
// the page never jumps under the pointer. A row with nothing to scroll leaves the wheel to the page.
// and leaves trackpad sideways gestures, shift+wheel and pinch-zoom to the browser.
const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches

export function wheelScroll(node) {
  let target = 0 // where the glide is heading
  let pos = 0 // where it is now (fractional, node.scrollLeft rounds)
  let raf = 0
  let last = 0
  let idle
  const snap = node.style.scrollSnapType

  const max = () => node.scrollWidth - node.clientWidth

  function stop() {
    cancelAnimationFrame(raf)
    raf = 0
    clearTimeout(idle)
    node.style.scrollSnapType = snap // hands control back, so the browser settles on a tile
  }
  function frame(now) {
    // exponential glide, same feel at 60 and 144 Hz
    pos += (target - pos) * (1 - Math.exp(-(now - last) / 80))
    last = now
    if (Math.abs(target - pos) < 0.5) pos = target
    node.scrollLeft = pos
    if (pos === target) {
      raf = 0
      idle = setTimeout(stop, 120) // snap stays off while wheel notches keep arriving
    } else raf = requestAnimationFrame(frame)
  }

  function wheel(e) {
    if (e.ctrlKey || e.shiftKey || e.deltaY === 0 || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return
    const room = max()
    if (room <= 0) return
    const gliding = raf !== 0
    if (!gliding) target = pos = node.scrollLeft
    const dir = Math.sign(e.deltaY)
    const atEdge = dir > 0 ? target >= room - 1 : target <= 1
    e.preventDefault()
    if (atEdge) return // already at the end that way: nothing to do, and the page stays put
    const px = e.deltaMode === 1 ? e.deltaY * 40 : e.deltaMode === 2 ? e.deltaY * node.clientWidth : e.deltaY
    target = Math.min(Math.max(target + px, 0), room)
    if (reduced()) {
      node.scrollLeft = target
      return
    }
    clearTimeout(idle)
    node.style.scrollSnapType = 'none'
    if (!raf) {
      last = performance.now()
      raf = requestAnimationFrame(frame)
    }
  }
  // dragging the scrollbar, touch, keys and any other scroll source cancel the glide
  const cancel = () => raf && stop()

  node.addEventListener('wheel', wheel, { passive: false })
  node.addEventListener('pointerdown', cancel)
  node.addEventListener('touchstart', cancel, { passive: true })
  node.addEventListener('keydown', cancel)
  return {
    destroy() {
      stop()
      node.removeEventListener('wheel', wheel)
      node.removeEventListener('pointerdown', cancel)
      node.removeEventListener('touchstart', cancel)
      node.removeEventListener('keydown', cancel)
    },
  }
}
