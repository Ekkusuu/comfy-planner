<script>
  // One icon set: 24px grid, 1.6 stroke (see svg.i in app.css).
  let { name, size } = $props()
  const solid = new Set(['spark']) // drawn filled rather than as a line: its points are too fine for a stroke at button size
  const paths = {
    spark: 'M10.5 5Q11.8 12.2 19 13.5Q11.8 14.8 10.5 22Q9.2 14.8 2 13.5Q9.2 12.2 10.5 5ZM18.5 2Q18.9 5.1 22 5.5Q18.9 5.9 18.5 9Q18.1 5.9 15 5.5Q18.1 5.1 18.5 2Z', // filled, see `solid`
    plan: 'M4 6h16M4 12h10M4 18h7',
    queue: 'M12 3 3 7.5l9 4.5 9-4.5zM3 12l9 4.5 9-4.5M3 16.5 12 21l9-4.5',
    gallery: 'M4.5 3h5A1.5 1.5 0 0 1 11 4.5v5A1.5 1.5 0 0 1 9.5 11h-5A1.5 1.5 0 0 1 3 9.5v-5A1.5 1.5 0 0 1 4.5 3zM14.5 3h5A1.5 1.5 0 0 1 21 4.5v5a1.5 1.5 0 0 1-1.5 1.5h-5A1.5 1.5 0 0 1 13 9.5v-5A1.5 1.5 0 0 1 14.5 3zM4.5 13h5a1.5 1.5 0 0 1 1.5 1.5v5A1.5 1.5 0 0 1 9.5 21h-5A1.5 1.5 0 0 1 3 19.5v-5A1.5 1.5 0 0 1 4.5 13zM14.5 13h5a1.5 1.5 0 0 1 1.5 1.5v5a1.5 1.5 0 0 1-1.5 1.5h-5a1.5 1.5 0 0 1-1.5-1.5v-5a1.5 1.5 0 0 1 1.5-1.5z',
    library: 'M5 19.5A2.5 2.5 0 0 1 7.5 17H20V3H7.5A2.5 2.5 0 0 0 5 5.5zM5 19.5A2.5 2.5 0 0 0 7.5 22H20v-5',
    settings: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M2 14h4M10 8h4M18 16h4',
    lock: 'M6 11h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM8 11V7a4 4 0 0 1 8 0v4',
    unlock: 'M6 11h12a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM8 11V7a4 4 0 0 1 7.5-2',
    plus: 'M12 5v14M5 12h14',
    minus: 'M5 12h14',
    play: 'M7 4l13 8-13 8z',
    x: 'M6 6l12 12M18 6 6 18',
    check: 'M5 12.5l4.5 4.5L19 7.5',
    chevron: 'M6 9l6 6 6-6',
    chevleft: 'M15 6l-6 6 6 6',
    chevright: 'M9 6l6 6-6 6',
    up: 'M6 15l6-6 6 6',
    alert: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 8v5M12 16h.01',
    more: 'M5 12h.01M12 12h.01M19 12h.01',
    copy: 'M9 9h10a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2zM5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1',
    trash: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6',
    retry: 'M21 12a9 9 0 1 1-2.6-6.4M21 4v5h-5',
    grip: 'M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01',
    image: 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM21 16l-5-5L5 21',
    upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
    import: 'M12 3v12M7 10l5 5 5-5M4 21h16',
    loader: 'M12 3a9 9 0 0 1 9 9',
    folder: 'M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
    external:'M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5',
    wand: 'M15 4l5 5L9 20H4v-5zM13 6l5 5',
    info: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 11v5.5M12 7.6h.01',
    book: 'M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5zM4 20.5A1.5 1.5 0 0 0 5.5 22H20v-4M9 8h7',
    box: 'M12 3 3 7.5v9L12 21l9-4.5v-9zM3 7.5l9 4.5 9-4.5M12 12v9',
    palette: 'M12 3a9 9 0 1 0 0 18c1.1 0 1.8-.8 1.8-1.7 0-.5-.2-.9-.5-1.3-.3-.4-.5-.8-.5-1.3 0-1 .8-1.7 1.7-1.7H17a4 4 0 0 0 4-4c0-4.4-4-8-9-8zM7.5 11h.01M10 7.5h.01M14.5 7.5h.01',
    user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0',
    bulb: 'M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z',
    shirt: 'M8 3 3 6l2.5 4L8 9v12h8V9l2.5 1L21 6l-5-3a4 4 0 0 1-8 0z',
    figure: 'M12 5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3zM12 6.5v7M12 9 7 7.5M12 9l5-1.5M12 13.5 9 21M12 13.5l3 7.5',
    tag: 'M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9zM8 8h.01',
    search: 'M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14zM21 21l-4.3-4.3',
  }
</script>

<svg class="i" class:solid={solid.has(name)} viewBox="0 0 24 24" aria-hidden="true" style={size ? `width:${size}px;height:${size}px` : undefined}>
  <path d={paths[name]} />
</svg>
