"""Charts for leaderboard graphs: one geometry, two outputs (SVG and PNG).

:func:`layout` turns a graph (:func:`rsostb.leaderboard.views.graph`) into a
list of drawing primitives; :func:`to_svg` and :func:`to_png` draw them, so an
exported PNG shows exactly what the SVG shows. Chart types: ``bar``
(horizontal, grouped), ``column`` (vertical, grouped), ``dot``, ``radar``,
``line`` (multi-line) and ``scatter``.

Colours follow the reference data-viz palette: categorical slots in a fixed
order (an entry keeps its colour whatever else is shown), the comparison
series ("all others: median / best") in neutral greys, text in text colours.
Every mark carries a tooltip (SVG ``<title>``), a legend is drawn for two or
more series, and the numbers are always available as a table (CSV export).
The PNG needs Pillow (shipped with Gradio); SVG needs nothing.
"""
from __future__ import annotations

import html
import io
import math
from typing import Any

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
NEUTRAL = {"median": "#898781", "best": "#52514e", "peer": "#b5b3ad"}
SURFACE, TEXT, TEXT2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#6f6e69", "#e5e4e0"
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
W = 880
MAX_SERIES = len(SERIES)


def _color(s: dict[str, Any], i: int) -> str:
    return NEUTRAL.get(s.get("role", ""), SERIES[i % len(SERIES)])


def _fmt(v: float | None, unit: str | None = None) -> str:
    if v is None:
        return "—"
    s = f"{v:,.1f}" if abs(v) < 1000 else f"{v:,.0f}"
    return s + ("%" if unit == "%" else "")


def _tick(v: float, unit: str | None = None) -> str:
    s = f"{v:,.0f}" if float(v).is_integer() or abs(v) >= 100 else f"{v:,.2f}".rstrip("0").rstrip(".")
    return s.replace("-", "−") + ("%" if unit == "%" else "")


def _nice(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, t = [], start
    while t <= hi + step * 0.5 and len(ticks) < 12:
        ticks.append(round(t, 10))
        t += step
    if ticks[-1] < hi:
        ticks.append(round(ticks[-1] + step, 10))
    return ticks


def _domain(values: list[float], unit: str | None) -> list[float]:
    vals = [v for v in values if v is not None]
    lo, hi = min([0.0, *vals]), max([0.0, *vals]) if vals else 1.0
    if unit == "%":
        hi = max(hi, 100.0) if hi > 50 else max(hi, 10.0)
    return _nice(lo, hi)


def _wrap(text: str, n: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > n:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + ([cur] if cur else [])


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


class Canvas:
    def __init__(self, width: int, height: int):
        self.w, self.h, self.ops = width, height, []

    def rect(self, x, y, w, h, fill, title=None, round_end=None):
        self.ops.append(("rect", x, y, max(w, 0), max(h, 0), fill, title, round_end))

    def line(self, x1, y1, x2, y2, stroke, width=1.0, dash=False):
        self.ops.append(("line", x1, y1, x2, y2, stroke, width, dash))

    def circle(self, cx, cy, r, fill, title=None, ring=True):
        self.ops.append(("circle", cx, cy, r, fill, title, ring))

    def poly(self, pts, stroke, width=2.0, fill=None, closed=False, dash=False):
        self.ops.append(("poly", pts, stroke, width, fill, closed, dash))

    def text(self, x, y, s, size=12, fill=TEXT2, anchor="start", bold=False, angle=0):
        self.ops.append(("text", x, y, s, size, fill, anchor, bold, angle))


def _header(c: Canvas, g: dict[str, Any], series: list[dict[str, Any]]) -> float:
    lined = g.get("chart") in ("line", "radar")
    c.text(16, 26, _clip(g.get("title") or "", 100), 16, TEXT, bold=True)
    y = 46
    for n in (g.get("notes") or [])[:3]:
        for part in _wrap(n, 125)[:2]:
            c.text(16, y, part, 11, MUTED)
            y += 16
    if len(series) >= 2:
        x = 16
        for i, s in enumerate(series):
            col = _color(s, i)
            label = _clip(s["name"], 60)
            width = 28 + 6.6 * len(label)
            if x > 16 and x + width > W - 16:
                x, y = 16, y + 18
            if lined:
                c.line(x, y + 1, x + 14, y + 1, col, 2, dash=s.get("role") == "best")
            else:
                c.rect(x, y - 4, 14, 10, col)
            c.text(x + 20, y + 5, label, 12, TEXT2)
            x += width
        y += 18
    return y + 8


def _height_for(g: dict[str, Any]) -> int:
    n_dim = max(1, len(g.get("dimensions") or []))
    n_ser = max(1, len(g.get("series") or []))
    note_lines = sum(min(2, len(_wrap(n, 125))) for n in (g.get("notes") or [])[:3])
    extra = 16 * note_lines + (18 * math.ceil(n_ser / 2) + 4 if n_ser >= 2 else 0)
    chart = g["chart"]
    if chart in ("bar", "dot"):
        band = max(22, n_ser * 9 + 10) if chart == "bar" else 22
        return int(70 + extra + n_dim * band + 30)
    if chart == "radar":
        return int((680 if n_dim > 16 else 560) + extra)
    return int(440 + extra)


def layout(g: dict[str, Any]) -> Canvas:
    series = (g.get("series") or [])[:MAX_SERIES]
    c = Canvas(W, _height_for(g))
    c.rect(0, 0, c.w, c.h, SURFACE)
    top = _header(c, g, series)
    chart = g["chart"]
    if chart == "scatter":
        _scatter(c, g, top)
    elif not g.get("dimensions") or not series:
        c.text(16, top + 20, "Nothing to show yet.", 13, MUTED)
    elif chart in ("bar", "dot"):
        _horizontal(c, g, series, top, dots=chart == "dot")
    elif chart == "column":
        _columns(c, g, series, top)
    elif chart == "line":
        _lines(c, g, series, top)
    elif chart == "radar":
        _radar(c, g, series, top)
    return c


def _horizontal(c: Canvas, g, series, top, dots: bool):
    unit, dims = g.get("unit"), g["dimensions"]
    ticks = _domain([v for s in series for v in s["values"]], unit)
    lo, hi = ticks[0], ticks[-1]
    x0, x1 = 230, W - 30
    band = 22 if dots else max(22, len(series) * 9 + 10)
    y_top = top + 18
    y_end = y_top + band * len(dims)

    def X(v):
        return x0 + (v - lo) / (hi - lo) * (x1 - x0)

    for t in ticks:
        c.line(X(t), y_top - 4, X(t), y_end, GRID, 1)
        c.text(X(t), y_end + 16, _tick(t, unit), 11, MUTED, "middle")
    c.line(X(0), y_top - 4, X(0), y_end, TEXT2, 1)
    for di, d in enumerate(dims):
        yb = y_top + di * band
        c.text(x0 - 8, yb + band / 2 + 4, _clip(d, 32), 12, TEXT2, "end")
        if dots:
            c.line(x0, yb + band / 2, x1, yb + band / 2, GRID, 1)
        thick = min(14.0, (band - 6) / len(series) - 2)
        pad = (band - len(series) * (thick + 2)) / 2
        for si, s in enumerate(series):
            v = s["values"][di]
            if v is None:
                continue
            tip = f"{s['name']} · {d}: {_fmt(v, unit)}"
            if dots:
                c.circle(X(v), yb + band / 2, 5, _color(s, si), tip)
            else:
                y = yb + pad + si * (thick + 2)
                xa, xb = sorted((X(0), X(v)))
                c.rect(xa, y, xb - xa, thick, _color(s, si), tip, round_end="right" if v >= 0 else "left")


def _vertical_axis(c: Canvas, ticks, unit, x0, x1, y0, y1):
    lo, hi = ticks[0], ticks[-1]

    def Y(v):
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    for t in ticks:
        c.line(x0, Y(t), x1, Y(t), GRID, 1)
        c.text(x0 - 8, Y(t) + 4, _tick(t, unit), 11, MUTED, "end")
    c.line(x0, Y(0), x1, Y(0), TEXT2, 1)
    return Y


def _columns(c: Canvas, g, series, top):
    unit, dims = g.get("unit"), g["dimensions"]
    ticks = _domain([v for s in series for v in s["values"]], unit)
    x0, x1, y0, y1 = 70, W - 20, top + 10, c.h - 120
    Y = _vertical_axis(c, ticks, unit, x0, x1, y0, y1)
    band = (x1 - x0) / len(dims)
    thick = max(2.0, min(28.0, (band - 6) / len(series) - 2))
    pad = (band - len(series) * (thick + 2)) / 2
    for di, d in enumerate(dims):
        xb = x0 + di * band
        c.text(xb + band / 2, y1 + 14, _clip(d, 22), 11, TEXT2, "end", angle=-40)
        for si, s in enumerate(series):
            v = s["values"][di]
            if v is None:
                continue
            x = xb + pad + si * (thick + 2)
            ya, yb_ = sorted((Y(0), Y(v)))
            c.rect(x, ya, thick, yb_ - ya, _color(s, si), f"{s['name']} · {d}: {_fmt(v, unit)}",
                   round_end="top" if v >= 0 else "bottom")


def _lines(c: Canvas, g, series, top):
    unit, dims = g.get("unit"), g["dimensions"]
    ticks = _domain([v for s in series for v in s["values"]], unit)
    x0, x1, y0, y1 = 70, W - 30, top + 10, c.h - 120
    Y = _vertical_axis(c, ticks, unit, x0, x1, y0, y1)
    step = (x1 - x0) / max(1, len(dims) - 1) if len(dims) > 1 else 0

    def X(i):
        return x0 + i * step if len(dims) > 1 else (x0 + x1) / 2

    for di, d in enumerate(dims):
        c.text(X(di), y1 + 14, _clip(d, 22), 11, TEXT2, "end", angle=-40)
    for si, s in enumerate(series):
        col = _color(s, si)
        run: list[tuple[float, float]] = []
        for di, v in enumerate(s["values"] + [None]):
            if v is None:
                if len(run) > 1:
                    c.poly(run, col, 2, dash=s.get("role") == "best")
                run = []
                continue
            run.append((X(di), Y(v)))
        for di, v in enumerate(s["values"]):
            if v is not None:
                c.circle(X(di), Y(v), 4, col, f"{s['name']} · {dims[di]}: {_fmt(v, unit)}")


def _radar(c: Canvas, g, series, top):
    unit, dims = g.get("unit"), g["dimensions"]
    if len(dims) < 3:
        c.text(16, top + 20, "A radar chart needs at least three dimensions; try bar or line.", 13, MUTED)
        return
    ticks = _domain([v for s in series for v in s["values"]], unit)
    lo, hi = ticks[0], ticks[-1]
    cx, cy, R = W / 2, top + (c.h - top) / 2, min(W / 2 - 170, (c.h - top) / 2 - (90 if len(dims) > 16 else 50))

    def P(i, v):
        a = -math.pi / 2 + 2 * math.pi * i / len(dims)
        r = (v - lo) / (hi - lo) * R
        return cx + r * math.cos(a), cy + r * math.sin(a)

    for t in ticks[1:]:
        c.poly([P(i, t) for i in range(len(dims))], GRID, 1, closed=True)
        x, y = P(0, t)
        c.text(x + 4, y + 4, _tick(t, unit), 10, MUTED)
    dense = len(dims) > 16          # many spokes: smaller labels at two alternating distances
    for i, d in enumerate(dims):
        x, y = P(i, hi)
        c.line(cx, cy, x, y, GRID, 1)
        ax, ay = P(i, hi + (hi - lo) * (0.06 + (0.16 if dense and i % 2 else 0)))
        anchor = "middle" if abs(ax - cx) < 20 else ("start" if ax > cx else "end")
        c.text(ax, ay + 4, _clip(d, 22 if dense else 24), 9 if dense else 11, TEXT2, anchor)
    for si, s in enumerate(series):
        col = _color(s, si)
        vals = s["values"]
        dash = s.get("role") == "best"
        if all(v is not None for v in vals):
            c.poly([P(i, v) for i, v in enumerate(vals)], col, 2,
                   fill=col if s.get("role") not in ("median", "best") else None, closed=True, dash=dash)
        else:   # a missing value is a gap, not a zero: outline only between known neighbours
            for i in range(len(vals)):
                j = (i + 1) % len(vals)
                if vals[i] is not None and vals[j] is not None:
                    c.poly([P(i, vals[i]), P(j, vals[j])], col, 2, dash=dash)
        for i, v in enumerate(s["values"]):
            if v is not None:
                c.circle(*P(i, v), 3.5, col, f"{s['name']} · {dims[i]}: {_fmt(v, unit)}")


def _scatter(c: Canvas, g, top):
    pts = [p for p in g.get("points") or [] if p["x"] is not None and p["y"] is not None]
    if not pts:
        c.text(16, top + 20, "No entry has both values.", 13, MUTED)
        return
    xt = _nice(min(0.0, *[p["x"] for p in pts]), max(0.0, *[p["x"] for p in pts]))
    yt = _nice(min(0.0, *[p["y"] for p in pts]), max(0.0, *[p["y"] for p in pts]))
    x0, x1, y0, y1 = 90, W - 30, top + 30, c.h - 60
    Y = _vertical_axis(c, yt, None, x0, x1, y0, y1)

    def X(v):
        return x0 + (v - xt[0]) / (xt[-1] - xt[0]) * (x1 - x0)

    for t in xt:
        c.line(X(t), y0, X(t), y1, GRID, 1)
        c.text(X(t), y1 + 16, _tick(t), 11, MUTED, "middle")
    c.text((x0 + x1) / 2, c.h - 18, g.get("x_label") or "", 12, TEXT2, "middle")
    c.text(16, y0 - 16, f"↑ {g.get('y_label') or ''}", 12, TEXT2)
    colored = [p for p in pts if p["role"] != "peer"]
    order = sorted(pts, key=lambda p: p["role"] != "peer")       # peers underneath
    for p in order:
        if p["role"] == "peer":
            col = NEUTRAL["peer"]
        else:
            idx = colored.index(p)
            col = SERIES[idx] if idx < 3 else NEUTRAL["median"]   # three hues validate all-pairs
        c.circle(X(p["x"]), Y(p["y"]), 5, col, f"{p['name']}: {_fmt(p['x'])}, {_fmt(p['y'])}")
    for p in colored[:4]:
        c.text(X(p["x"]) + 8, Y(p["y"]) - 8, _clip(p["name"], 30), 11, TEXT)


# --------------------------------------------------------------------------- backends

def _round_rect_path(x, y, w, h, end, r=4.0):
    r = min(r, w / 2, h / 2)
    if end == "right":
        return f"M{x},{y}h{w - r}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r}a{r},{r} 0 0 1 {-r},{r}h{-(w - r)}z"
    if end == "left":
        return f"M{x + w},{y}v{h}h{-(w - r)}a{r},{r} 0 0 1 {-r},{-r}v{-(h - 2 * r)}a{r},{r} 0 0 1 {r},{-r}z"
    if end == "top":
        return f"M{x},{y + h}v{-(h - r)}a{r},{r} 0 0 1 {r},{-r}h{w - 2 * r}a{r},{r} 0 0 1 {r},{r}v{h - r}z"
    return f"M{x},{y}h{w}v{h - r}a{r},{r} 0 0 1 {-r},{r}h{-(w - 2 * r)}a{r},{r} 0 0 1 {-r},{-r}z"


def _n(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def to_svg(g: dict[str, Any]) -> str:
    c = layout(g)
    e = html.escape
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {c.w} {c.h}" width="{c.w}" height="{c.h}" '
           f'role="img" aria-label="{e(g.get("title") or "chart")}" font-family="{e(FONT)}">']
    for op in c.ops:
        k = op[0]
        if k == "rect":
            _, x, y, w, h, fill, tip, end = op
            shape = (f'<path d="{_round_rect_path(x, y, w, h, end)}" fill="{fill}"' if end and w > 1 and h > 1
                     else f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}" fill="{fill}"')
            out.append(shape + (f"><title>{e(tip)}</title></{'path' if end and w > 1 and h > 1 else 'rect'}>"
                                if tip else "/>"))
        elif k == "line":
            _, x1, y1, x2, y2, stroke, width, dash = op
            out.append(f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" stroke="{stroke}" '
                       f'stroke-width="{width}"' + (' stroke-dasharray="5 4"' if dash else "") + "/>")
        elif k == "circle":
            _, cx, cy, r, fill, tip, ring = op
            out.append(f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{r}" fill="{fill}"'
                       + (f' stroke="{SURFACE}" stroke-width="2"' if ring else "")
                       + (f"><title>{e(tip)}</title></circle>" if tip else "/>"))
        elif k == "poly":
            _, pts, stroke, width, fill, closed, dash = op
            tag = "polygon" if closed else "polyline"
            out.append(f'<{tag} points="{" ".join(f"{_n(x)},{_n(y)}" for x, y in pts)}" stroke="{stroke}" '
                       f'stroke-width="{width}" stroke-linejoin="round" '
                       f'fill="{fill or "none"}"' + (' fill-opacity="0.12"' if fill else "")
                       + (' stroke-dasharray="5 4"' if dash else "") + "/>")
        elif k == "text":
            _, x, y, s, size, fill, anchor, bold, angle = op
            rot = f' transform="rotate({angle} {_n(x)} {_n(y)})"' if angle else ""
            out.append(f'<text x="{_n(x)}" y="{_n(y)}" font-size="{size}" fill="{fill}" text-anchor="{anchor}"'
                       + (' font-weight="600"' if bold else "") + f"{rot}>{e(str(s))}</text>")
    out.append("</svg>")
    return "\n".join(out)


def _hex(col: str, alpha: int = 255) -> tuple[int, int, int, int]:
    col = col.lstrip("#")
    return int(col[0:2], 16), int(col[2:4], 16), int(col[4:6], 16), alpha


def _font(size: float, bold: bool = False):
    from PIL import ImageFont

    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), "Arial.ttf"):
        try:
            return ImageFont.truetype(name, int(size))
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=int(size))
    except TypeError:
        return ImageFont.load_default()


def to_png(g: dict[str, Any], scale: int = 2) -> bytes:
    """The same chart as a PNG (Pillow), at ``scale`` × resolution."""
    from PIL import Image, ImageDraw

    c = layout(g)
    img = Image.new("RGBA", (c.w * scale, c.h * scale), _hex(SURFACE))
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d, do = ImageDraw.Draw(img), ImageDraw.Draw(over)
    S = scale

    def dashed(p1, p2, fill, width):
        (x1, y1), (x2, y2) = p1, p2
        length = math.hypot(x2 - x1, y2 - y1)
        n = max(1, int(length / (9 * S)))
        for i in range(n):
            a, b = i / n, min(1.0, (i + 5 / 9) / n)
            d.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a), (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)],
                   fill=fill, width=width)

    for op in c.ops:
        k = op[0]
        if k == "rect":
            _, x, y, w, h, fill, _tip, end = op
            box = [round(x * S), round(y * S), round((x + w) * S), round((y + h) * S)]
            radius = int(min(4 * S, (box[2] - box[0]) / 2 - 1, (box[3] - box[1]) / 2 - 1))
            if end and radius >= 1:
                corners = {"right": (False, True, True, False), "left": (True, False, False, True),
                           "top": (True, True, False, False), "bottom": (False, False, True, True)}[end]
                d.rounded_rectangle(box, radius=radius, fill=_hex(fill), corners=corners)
            elif box[2] > box[0] and box[3] > box[1]:
                d.rectangle(box, fill=_hex(fill))
        elif k == "line":
            _, x1, y1, x2, y2, stroke, width, dash = op
            (dashed if dash else lambda a, b, f, w: d.line([a, b], fill=f, width=w))(
                (x1 * S, y1 * S), (x2 * S, y2 * S), _hex(stroke), max(1, int(width * S)))
        elif k == "circle":
            _, cx, cy, r, fill, _tip, ring = op
            if ring:
                d.ellipse([(cx - r - 2) * S, (cy - r - 2) * S, (cx + r + 2) * S, (cy + r + 2) * S], fill=_hex(SURFACE))
            d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S], fill=_hex(fill))
        elif k == "poly":
            _, pts, stroke, width, fill, closed, dash = op
            P = [(x * S, y * S) for x, y in pts]
            if fill:
                do.polygon(P, fill=_hex(fill, 31))
            seq = P + ([P[0]] if closed else [])
            for a, b in zip(seq, seq[1:]):
                (dashed if dash else lambda a, b, f, w: d.line([a, b], fill=f, width=w))(
                    a, b, _hex(stroke), max(1, int(width * S)))
        elif k == "text":
            _, x, y, s, size, fill, anchor, bold, angle = op
            font = _font(size * S, bold)
            anchor_pil = {"start": "ls", "middle": "ms", "end": "rs"}[anchor]
            if not angle:
                d.text((x * S, y * S), str(s), fill=_hex(fill), font=font, anchor=anchor_pil)
            else:
                tw = int(d.textlength(str(s), font=font)) + 4
                th = int(size * S * 1.4)
                tmp = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
                ImageDraw.Draw(tmp).text((tw if anchor == "end" else 0, th * 0.8), str(s), fill=_hex(fill),
                                         font=font, anchor="rs" if anchor == "end" else "ls")
                rot = tmp.rotate(-angle, expand=True, resample=Image.BICUBIC)
                # anchor the end of the text at (x, y): for -40°, the end is the top-right corner
                px = int(x * S - rot.width) if anchor == "end" else int(x * S)
                img.alpha_composite(rot, (max(0, px), max(0, int(y * S - th * 0.6))))
    img = Image.alpha_composite(img, over)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()
