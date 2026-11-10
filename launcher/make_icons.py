"""Draws the Comfy Planner mark (mint tile, ink "C" ring with a dot in its opening) at any size.
Geometry lives on a 64 grid so the SVG, PNGs and ICO all come from the same numbers.
Run: uv run --with pillow python launcher/make_icons.py
"""
import math, io, struct, pathlib
from PIL import Image, ImageDraw

MINT, INK = (110, 231, 183, 255), (6, 42, 30, 255)
TILE_R = 14.5          # tile corner radius (64 grid)
RING_R, RING_W = 15.0, 7.0
GAP = 38               # half-angle of the opening (degrees), opening faces right
DOT = (46.0, 32.0, 4.6)  # cx, cy, r

def pt(r, deg):  # point on the ring, y down
    a = math.radians(deg)
    return 32 + r * math.cos(a), 32 + r * math.sin(a)

def svg():
    x1, y1 = pt(RING_R, GAP)
    x2, y2 = pt(RING_R, -GAP)
    # arc from lower end, the long way round (through the left), to the upper end
    ring = f"M{x1:.2f} {y1:.2f}A{RING_R} {RING_R} 0 1 1 {x2:.2f} {y2:.2f}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            f'<rect x="1" y="1" width="62" height="62" rx="{TILE_R}" fill="#6EE7B7"/>'
            f'<path d="{ring}" fill="none" stroke="#062A1E" stroke-width="{RING_W}"/>'
            f'<circle cx="{DOT[0]}" cy="{DOT[1]}" r="{DOT[2]}" fill="#062A1E"/></svg>')

def render(size, ss=8):
    n = size * ss
    k = n / 64
    im = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([1 * k, 1 * k, 63 * k, 63 * k], radius=TILE_R * k, fill=MINT)
    # ring as a thick arc: PIL angles run clockwise from 3 o'clock, same as our y-down math
    o, i = RING_R + RING_W / 2, RING_R - RING_W / 2
    ring = Image.new("L", (n, n), 0)
    rd = ImageDraw.Draw(ring)
    rd.pieslice([(32 - o) * k, (32 - o) * k, (32 + o) * k, (32 + o) * k], GAP, 360 - GAP, fill=255)
    rd.ellipse([(32 - i) * k, (32 - i) * k, (32 + i) * k, (32 + i) * k], fill=0)
    im.paste(Image.new("RGBA", (n, n), INK), (0, 0), ring)
    d.ellipse([(DOT[0] - DOT[2]) * k, (DOT[1] - DOT[2]) * k, (DOT[0] + DOT[2]) * k, (DOT[1] + DOT[2]) * k], fill=INK)
    return im.resize((size, size), Image.LANCZOS)

def ico(sizes):
    pngs = []
    for s in sizes:
        b = io.BytesIO(); render(s).save(b, "PNG", optimize=True); pngs.append(b.getvalue())
    out = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes)
    for s, p in zip(sizes, pngs):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(p), off); off += len(p)
    return out + b"".join(pngs)

if __name__ == "__main__":
    root = pathlib.Path(__file__).resolve().parent.parent
    pub = root / "ui" / "public"
    (pub / "logo.svg").write_text(svg(), encoding="utf-8")
    for name, s in [("favicon-32.png", 32), ("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-256.png", 256), ("icon-512.png", 512)]:
        render(s).save(pub / name, optimize=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    (pub / "favicon.ico").write_bytes(ico(sizes))
    (root / "launcher" / "icon.ico").write_bytes(ico(sizes))
    (root / "scratch" / "screens").mkdir(parents=True, exist_ok=True)
    render(512).save(root / "scratch" / "screens" / "mark-512.png")
    # contact sheet at real sizes on the dark app colour
    sheet = Image.new("RGBA", (420, 140), (10, 10, 11, 255)); x = 12
    for s in (16, 24, 32, 48, 64, 128):
        sheet.paste(render(s), (x, 6), render(s)); x += s + 14
    sheet.resize((840, 280), Image.NEAREST).save(root / "scratch" / "screens" / "mark-sizes.png")
    print("ok")
