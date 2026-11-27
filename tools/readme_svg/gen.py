#!/usr/bin/env python3
"""Generate the README SVGs in light and dark variants from one set of tokens.

Design system (viewBox units; every SVG is 660 wide and shown at 440 px on desktop):
  type: 28 regular (body, labels) / 32 semibold (titles, values). Two sizes only.
        28 units shows at 18.7 px on desktop, 14.8 px in a 400 px phone (about 350 px of image width)
        and 13.1 px on a 360 px phone. The review round raised it from 24/28 at 560 px, which was
        12.7 px on a 400 px phone and under 10 px at 320 px.
  run: python3 -I tools/readme_svg/gen.py (from the repo root; standard library only).
  long text: notes that run to several lines live in the README as real text, not in the images.
  spacing: multiples of 4.
  colour: one neutral ramp (ink, muted, line) plus one accent (teal), each with a dark-theme step.
          Contrast ratios (WCAG 2) are noted beside each token.
  status: shape and words always carry it (filled tick, dashed ring, dash; group titles; "provisional"),
          never colour alone.
"""

from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "assets" / "readme"

THEMES = {
    "light": {
        "ink": "#1f2328",  # 15.8:1 on #ffffff
        "muted": "#59636e",  # 6.1:1
        "line": "#d1d9e0",  # hairlines only, never text
        "accent": "#0b6b69",  # 6.3:1, marks and motif details
        "accent_soft": "#d5ecea",
        "on_accent": "#ffffff",
        "paper": "#ffffff",
    },
    "dark": {
        "ink": "#f0f6fc",  # 17.4:1 on #0d1117, 13.7:1 on dimmed #212830
        "muted": "#9198a1",  # 6.5:1, 5.1:1 on dimmed
        "line": "#3d444d",
        "accent": "#4fbfb6",  # 8.5:1
        "accent_soft": "#163b39",
        "on_accent": "#0d1117",
        "paper": "#0d1117",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, &apos;Segoe UI&apos;, &apos;Noto Sans&apos;, Helvetica, Arial, sans-serif"
W = 660
BODY, TITLE = 28, 32
LEAD = 36  # line step for wrapped body text


def svg(height: int, title: str, desc: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" height="{height}" '
        f'role="img" aria-labelledby="t d" font-family="{FONT}">\n'
        f'<title id="t">{title}</title>\n<desc id="d">{desc}</desc>\n{body}</svg>\n'
    )


def text(x, y, s, c, size=BODY, weight=400, anchor="start"):
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    w = f' font-weight="{weight}"' if weight != 400 else ""
    return f'<text x="{x}" y="{y}" font-size="{size}"{w} fill="{c}"{a}>{s}</text>\n'


# ---------------------------------------------------------------- 1. hero
def hero(t):
    ink, muted, line, acc = t["ink"], t["muted"], t["line"], t["accent"]
    sw = 'stroke-width="3" stroke-linecap="round" stroke-linejoin="round" fill="none"'
    # Motifs live in an 88x88 box and tell one paper story: a question, a published page being read, a result.
    # (The review replaced a culture plate: to an ID physician it reads as specimens, the opposite of
    # "public numbers only".)
    slip = (
        f'<rect x="18" y="8" width="52" height="72" rx="4" stroke="{muted}" {sw}/>'
        f'<path d="M30 24h28M30 34h20" stroke="{muted}" {sw}/>'
        f'<text x="44" y="70" font-size="28" font-weight="600" fill="{acc}" text-anchor="middle">?</text>'
    )
    look = (
        f'<rect x="8" y="8" width="52" height="72" rx="4" stroke="{muted}" {sw}/>'
        f'<path d="M20 22h28M20 32h28M20 42h18M20 52h12" stroke="{muted}" {sw}/>'
        f'<circle cx="54" cy="56" r="16" fill="{t["paper"]}" stroke="{acc}" stroke-width="4"/>'
        f'<path d="M66 68l12 12" stroke="{acc}" stroke-width="5" stroke-linecap="round"/>'
    )
    report = (
        f'<rect x="12" y="8" width="52" height="72" rx="4" stroke="{muted}" {sw}/>'
        f'<path d="M24 20h28M24 28h16" stroke="{muted}" {sw}/>'
        f'<text x="22" y="54" font-size="22" font-weight="600" fill="{ink}">174</text>'
        f'<circle cx="66" cy="72" r="13" fill="{t["paper"]}" stroke="{acc}" stroke-width="3"/>'
        f'<path d="M60 72l4 4 8-9" stroke="{acc}" {sw}/>'
    )
    rows = [
        (slip, "You ask Claude a question", ["In plain words, as you would", "ask a colleague"]),
        (look, "It checks official public data", ["CDC and WHO today, state", "health departments next"]),
        (report, "You get the number", ["With its source and the date", "it was checked"]),
    ]
    step = 148
    body = (
        f'<path d="M44 {96}V{step - 8}M44 {step + 96}V{2 * step - 8}" stroke="{line}" stroke-width="3" '
        f'stroke-linecap="round"/>\n'
    )
    for i, (motif, head, subs) in enumerate(rows):
        y = i * step
        body += f'<g transform="translate(0 {y})">{motif}</g>\n'
        body += text(112, y + 32, head, ink, TITLE, 600)
        for j, ln in enumerate(subs):
            body += text(112, y + 72 + j * LEAD, ln, muted)
    return svg(
        2 * step + 120,
        "How it works, in three steps",
        "You ask Claude a question in plain words. It checks official public data: CDC and WHO today, state health "
        "departments next. You get the number with its source and the date it was checked.",
        body,
    )


# ---------------------------------------------------------------- 2. coverage
def coverage(t):
    ink, muted, line, acc = t["ink"], t["muted"], t["line"], t["accent"]
    now = [
        ["Reportable infections by US state"],
        ["Antibiotic resistance in hospital", "infections"],
        ["COVID-19, flu and RSV hospital", "admissions"],
        ['<tspan font-style="italic">C. difficile</tspan> infections'],
        ["Tuberculosis by country (WHO)"],
    ]
    nxt = [["Measles cases by Michigan county"], ["Detroit Health Department figures"]]
    later = [["HIV"]]

    def mark_now(y):
        return (
            f'<circle cx="14" cy="{y}" r="13" fill="{acc}"/>'
            f'<path d="M8 {y}l4 4 8-8.5" stroke="{t["on_accent"]}" stroke-width="3" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"/>\n'
        )

    def mark_next(y):
        return f'<circle cx="14" cy="{y}" r="12" fill="none" stroke="{acc}" stroke-width="3" stroke-dasharray="5 4"/>\n'

    def mark_later(y):
        return f'<path d="M4 {y}h20" stroke="{muted}" stroke-width="3" stroke-linecap="round"/>\n'

    def group(title, note, items, mark, y):
        out = text(0, y, title, ink, TITLE, 600)
        if note:
            y += 40
            out += text(0, y, note, muted)
        y += 8
        for lines in items:
            y += 48
            out += mark(y - 10)
            for j, ln in enumerate(lines):
                out += text(44, y + j * LEAD, ln, ink)
            y += LEAD * (len(lines) - 1)
        return out, y

    body, y = "", 36
    g, y = group("Covered now", "From CDC, except where marked", now, mark_now, y)
    body += g
    for title, items, mark in [("Coming next", nxt, mark_next), ("Not covered yet", later, mark_later)]:
        y += 32
        body += f'<path d="M0 {y}H{W}" stroke="{line}" stroke-width="1"/>\n'
        y += 52
        g, y = group(title, None, items, mark, y)
        body += g
    return svg(
        y + 16,
        "What it covers",
        "Covered now, from CDC: reportable infections by US state; antibiotic resistance in hospital infections; "
        "COVID-19, flu and RSV hospital admissions; C. difficile infections. From WHO: tuberculosis by country. "
        "Coming next: measles cases by Michigan county; Detroit Health Department figures. Not covered yet: HIV.",
        body,
    )


# ---------------------------------------------------------------- 3. chart
def chart(t):
    ink, muted, line, acc, soft = t["ink"], t["muted"], t["line"], t["accent"], t["accent_soft"]
    # CDC NNDSS (data.cdc.gov x9gk-5huc), each year's m3 at its last week, checked 25 November 2026. CDC marks
    # 2024 and 2025 provisional; its 2025 table restates 2024 as 184 (m4), shown in the README text, not here.
    data = [("2022", 27, False), ("2023", 103, False), ("2024", 174, True), ("2025", 178, True)]
    base, h, vmax, bw = 380, 224, 200, 48
    body = (
        f'<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<rect width="8" height="8" fill="{soft}"/><rect width="3" height="8" fill="{acc}"/></pattern></defs>\n'
    )
    body += text(
        0, 36, 'Clinical <tspan font-style="italic">C. auris</tspan> cases, Michigan', ink, TITLE, 600
    )
    body += text(0, 76, "Reported each year, CDC year-end counts", muted)
    slot = W / 4
    for i, (yr, v, prov) in enumerate(data):
        cx = slot * i + slot / 2
        bh = v / vmax * h
        x0, top = cx - bw / 2, base - bh
        r = 4
        d = f"M{x0} {base}V{top + r}Q{x0} {top} {x0 + r} {top}H{x0 + bw - r}Q{x0 + bw} {top} {x0 + bw} {top + r}V{base}Z"
        body += f'<path d="{d}" fill="{"url(#hatch)" if prov else acc}"/>\n'
        body += text(cx, top - 12, str(v), ink, TITLE, 600, "middle")
        body += text(cx, base + 36, yr, muted, anchor="middle")
        if prov:
            body += text(cx, base + 72, "provisional", muted, anchor="middle")
    body += f'<path d="M0 {base}H{W}" stroke="{line}" stroke-width="2"/>\n'
    return svg(
        base + 88,
        "Example answer: clinical C. auris cases in Michigan",
        "Bar chart of clinical Candida auris cases reported in Michigan, CDC year-end counts, checked "
        "25 November 2026: 2022, 27; 2023, 103; 2024, 174, provisional; 2025, 178, provisional.",
        body,
    )


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in [("how-it-works", hero), ("coverage", coverage), ("example-c-auris-michigan", chart)]:
        for theme, tok in THEMES.items():
            (OUT / f"{name}-{theme}.svg").write_text(fn(tok), encoding="utf-8")
    print(sorted(p.name for p in OUT.iterdir()))
