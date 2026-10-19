"""Draws the app icon (original artwork) and writes icon.png / icon.icns / icon.ico.

Motif: a stack of rendered frames under a night sky with a crescent moon -
renders that keep going overnight. Run:  python3 tools/make_icon.py
"""
import os
import random

from PIL import Image, ImageDraw, ImageFilter

S = 2048                     # draw at 2x, downscale for smooth edges
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bbr", "assets")


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


def vgradient(w, h, top, bottom):
    g = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(g)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=lerp(top, bottom, y / max(1, h - 1)))
    return g


def dgradient(w, h, a, b):
    """diagonal gradient top-left -> bottom-right"""
    g = Image.new("RGBA", (w, h))
    px = g.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = lerp(a, b, (x / w + y / h) / 2)
    return g


def rmask(w, h, r):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    return m


def paste_rounded(base, img, xy, r, shadow=True):
    w, h = img.size
    if shadow:
        sh = Image.new("RGBA", (w + 160, h + 160), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([80, 80, 80 + w, 80 + h], radius=r, fill=(0, 0, 0, 120))
        sh = sh.filter(ImageFilter.GaussianBlur(36))
        base.alpha_composite(sh, (xy[0] - 80 + 14, xy[1] - 80 + 30))
    base.paste(img, xy, rmask(w, h, r))


def card(w, h, a, b, r, inner=None):
    c = dgradient(w, h, a, b) if w * h < 900_000 else vgradient(w, h, a, b)
    if inner:
        inner(c)
    return c


def main():
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # body: macOS grid - 1648px body on a 2048 canvas, ~22% corner radius
    m = 200
    bw = S - 2 * m
    body = vgradient(bw, bw, (22, 28, 52, 255), (40, 52, 98, 255))
    bd = ImageDraw.Draw(body)
    rnd = random.Random(7)
    mx, my, mr = int(bw * 0.76), int(bw * 0.22), 150
    for _ in range(46):                                   # stars
        x, y = rnd.randint(60, bw - 60), rnd.randint(60, int(bw * 0.55))
        if (x - mx) ** 2 + (y - my) ** 2 < 280 ** 2:
            continue
        rr = rnd.choice([3, 4, 5, 6, 8])
        bd.ellipse([x - rr, y - rr, x + rr, y + rr], fill=(255, 255, 255, rnd.randint(110, 230)))
    # crescent moon
    moon = Image.new("RGBA", (bw, bw), (0, 0, 0, 0))
    md = ImageDraw.Draw(moon)
    md.ellipse([mx - mr, my - mr, mx + mr, my + mr], fill=(255, 231, 168, 255))
    md.ellipse([mx - mr + 85, my - mr - 40, mx + mr + 85, my + mr - 40], fill=(0, 0, 0, 0))
    glow = moon.filter(ImageFilter.GaussianBlur(40))
    body.alpha_composite(glow)
    body.alpha_composite(moon)
    icon.paste(body, (m, m), rmask(bw, bw, int(bw * 0.225)))

    # stack of frames
    cw, ch, r = 980, 640, 70
    x0, y0 = m + 250, m + 760
    back2 = card(cw, ch, (70, 84, 140, 255), (52, 62, 110, 255), r)
    back1 = card(cw, ch, (98, 112, 176, 255), (74, 86, 146, 255), r)

    def front_art(c):
        d = ImageDraw.Draw(c)
        w, h = c.size
        # rolling hills in the "render"
        d.polygon([(0, h * 0.72), (w * 0.28, h * 0.52), (w * 0.52, h * 0.66), (w * 0.78, h * 0.46),
                   (w, h * 0.6), (w, h), (0, h)], fill=(120, 34, 92, 255))
        d.polygon([(0, h * 0.86), (w * 0.35, h * 0.7), (w * 0.7, h * 0.82), (w, h * 0.72), (w, h), (0, h)],
                  fill=(70, 22, 70, 255))
        d.ellipse([w * 0.18 - 70, h * 0.36 - 70, w * 0.18 + 70, h * 0.36 + 70], fill=(255, 238, 190, 255))
        # progress bar
        pad, bh = 70, 46
        d.rounded_rectangle([pad, h - pad - bh, w - pad, h - pad], radius=bh // 2, fill=(150, 70, 125, 255))
        d.rounded_rectangle([pad, h - pad - bh, pad + (w - 2 * pad) * 0.68, h - pad], radius=bh // 2,
                            fill=(255, 255, 255, 245))

    front = card(cw, ch, (255, 150, 72, 255), (238, 70, 118, 255), r, front_art)
    paste_rounded(icon, back2, (x0 + 170, y0 - 200), r)
    paste_rounded(icon, back1, (x0 + 85, y0 - 100), r)
    paste_rounded(icon, front, (x0, y0), r)

    os.makedirs(OUT, exist_ok=True)
    final = icon.resize((1024, 1024), Image.LANCZOS)
    final.save(os.path.join(OUT, "icon.png"))
    final.save(os.path.join(OUT, "icon.icns"))
    final.save(os.path.join(OUT, "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("written to", OUT)


if __name__ == "__main__":
    main()
