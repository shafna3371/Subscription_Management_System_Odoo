"""Tiny server-side SVG charts. Colours come from CSS classes in style.css,
so charts follow light and dark mode automatically."""
from markupsafe import Markup, escape


def _fmt(v):
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.1f}k"
    return f"{v:.0f}"


def line_chart(points, width=640, height=220):
    if not points:
        return Markup("")
    pad_l, pad_r, pad_t, pad_b = 48, 12, 14, 28
    values = [v for _, v in points]
    top = max(values) * 1.12 or 1
    w, h = width - pad_l - pad_r, height - pad_t - pad_b
    step = w / max(len(points) - 1, 1)
    xy = [(pad_l + i * step, pad_t + h - v / top * h) for i, (_, v) in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(xy))
    area = path + f" L{xy[-1][0]:.1f},{pad_t + h} L{xy[0][0]:.1f},{pad_t + h} Z"
    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="MRR over 12 months">']
    for frac in (0, 0.5, 1):
        y = pad_t + h - frac * h
        parts.append(f'<line x1="{pad_l}" x2="{width - pad_r}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        parts.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">{_fmt(top * frac)}</text>')
    parts.append(f'<path d="{area}" class="area"/><path d="{path}" class="line"/>')
    for i, ((label, v), (x, y)) in enumerate(zip(points, xy)):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" class="dot"><title>{escape(label)}: {v:,.2f}</title></circle>')
        if i % 2 == len(points) % 2 or i == len(points) - 1:
            parts.append(f'<text x="{x:.1f}" y="{height - 8}" class="axis" text-anchor="middle">{escape(label)}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def waterfall_chart(wf, width=640, height=240):
    steps = [("Start", wf["start"], "total"), ("New", wf["new"], "up"), ("Reactivated", wf["reactivation"], "up"),
             ("Expansion", wf["expansion"], "up"), ("Contraction", -wf["contraction"], "down"),
             ("Churn", -wf["churn"], "down"), ("End", wf["end"], "total")]
    pad_l, pad_t, pad_b = 48, 18, 30
    h = height - pad_t - pad_b
    running, bars = 0.0, []
    for label, v, kind in steps:
        if kind == "total":
            bars.append((label, 0.0, v, kind, v))
            running = v
        else:
            lo, hi = sorted((running, running + v))
            bars.append((label, lo, hi, kind, v))
            running += v
    top = max(b[2] for b in bars) * 1.1 or 1
    bottom = min(0.0, min(b[1] for b in bars))
    span = top - bottom
    slot = (width - pad_l - 10) / len(bars)

    def y(v):
        return pad_t + h - (v - bottom) / span * h

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="MRR movements this month">']
    parts.append(f'<line x1="{pad_l}" x2="{width - 10}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="grid"/>')
    for i, (label, lo, hi, kind, v) in enumerate(bars):
        x = pad_l + i * slot + slot * 0.18
        bw = slot * 0.64
        top_y, bar_h = y(hi), max(y(lo) - y(hi), 1.5)
        parts.append(f'<rect x="{x:.1f}" y="{top_y:.1f}" width="{bw:.1f}" height="{bar_h:.1f}" class="bar-{kind}">'
                     f'<title>{escape(label)}: {v:,.2f}</title></rect>')
        sign = "+" if kind == "up" and v else ""
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{top_y - 5:.1f}" class="value" text-anchor="middle">'
                     f'{sign}{_fmt(v)}</text>')
        parts.append(f'<text x="{x + bw / 2:.1f}" y="{height - 10}" class="axis" text-anchor="middle">{escape(label)}</text>')
    parts.append("</svg>")
    return Markup("".join(parts))


def bar_list(items, css="bar-neutral"):
    """Horizontal bars as HTML (label, value) - accessible and responsive."""
    top = max((v for _, v in items), default=0) or 1
    rows = []
    for label, v in items:
        pct = v / top * 100
        rows.append(f'<div class="hbar"><span class="hbar-label">{escape(label)}</span>'
                    f'<span class="hbar-track"><span class="hbar-fill {css}" style="width:{pct:.1f}%"></span></span>'
                    f'<span class="hbar-value num">{v:,.2f}</span></div>')
    return Markup("".join(rows))
