#!/usr/bin/env python3
"""Generate the README SVGs in light and dark variants from one set of tokens.

Design system (viewBox units; every SVG is 660 wide and shown at 440 px on desktop):
  type: 28 regular (body, labels) / 32 semibold (titles, values). Two sizes only.
        28 units shows at 18.7 px on desktop, 14.8 px in a 400 px phone (about 350 px of image width)
        and 13.1 px on a 360 px phone. The review round raised it from 24/28 at 560 px, which was
        12.7 px on a 400 px phone and under 10 px at 320 px.
  run: python3 -I tools/readme_svg/gen.py (from the repo root; standard library only). --check exits 1 if a
       committed SVG differs from what this file draws; tests/test_readme_svg.py runs the same check in CI.
  long text: notes that run to several lines live in the README as real text, not in the images.
  spacing: multiples of 4.
  colour: one neutral ramp (ink, muted, line) plus one accent (teal), each with a dark-theme step.
          Contrast ratios (WCAG 2) are noted beside each token.
  status: shape and words always carry it (filled tick, dashed ring, dash; group titles; "provisional"),
          never colour alone.
"""

import sys
from collections.abc import Callable
from pathlib import Path

Theme = dict[str, str]

OUT = Path(__file__).resolve().parents[2] / "assets" / "readme"

THEMES: dict[str, Theme] = {
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


def text(
    x: float, y: float, s: str, c: str, size: int = BODY, weight: int = 400, anchor: str = "start"
) -> str:
    a = f' text-anchor="{anchor}"' if anchor != "start" else ""
    w = f' font-weight="{weight}"' if weight != 400 else ""
    return f'<text x="{x}" y="{y}" font-size="{size}"{w} fill="{c}"{a}>{s}</text>\n'


# ---------------------------------------------------------------- 1. hero
def hero(t: Theme) -> str:
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
def coverage(t: Theme) -> str:
    ink, muted, line, acc = t["ink"], t["muted"], t["line"], t["accent"]
    # Only what catalog/ has: NNDSS lists every notifiable condition by state; NHSN AR is by state; RESP-NET gives
    # rates for its surveillance sites' catchment areas, not admissions; HAICViz CDI is pooled across EIP sites.
    now = [
        [
            "Notifiable infections by US state,",
            'from <tspan font-style="italic">C. auris</tspan> to syphilis',
        ],
        ["Antibiotic resistance in hospital", "infections, by state"],
        ["COVID-19, flu and RSV hospitalization", "rates at CDC surveillance sites"],
        [
            '<tspan font-style="italic">C. difficile</tspan> infections, national,',
            "from CDC surveillance sites",
        ],
        ["Tuberculosis and new HIV infections", "by country (WHO)"],
    ]
    nxt = [["Measles cases by Michigan county"], ["Detroit Health Department figures"]]
    later = [["HIV in the United States"]]

    def mark_now(y: float) -> str:
        return (
            f'<circle cx="14" cy="{y}" r="13" fill="{acc}"/>'
            f'<path d="M8 {y}l4 4 8-8.5" stroke="{t["on_accent"]}" stroke-width="3" fill="none" '
            f'stroke-linecap="round" stroke-linejoin="round"/>\n'
        )

    def mark_next(y: float) -> str:
        return f'<circle cx="14" cy="{y}" r="12" fill="none" stroke="{acc}" stroke-width="3" stroke-dasharray="5 4"/>\n'

    def mark_later(y: float) -> str:
        return f'<path d="M4 {y}h20" stroke="{muted}" stroke-width="3" stroke-linecap="round"/>\n'

    def group(
        title: str, note: str | None, items: list[list[str]], mark: Callable[[float], str], y: float
    ) -> tuple[str, float]:
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

    body, y = "", 36.0
    g, y = group("Covered now", "From CDC, except where marked", now, mark_now, y)
    body += g
    for title, items, mark in [("Coming next", nxt, mark_next), ("Not covered yet", later, mark_later)]:
        y += 32
        body += f'<path d="M0 {y}H{W}" stroke="{line}" stroke-width="1"/>\n'
        y += 52
        g, y = group(title, None, items, mark, y)
        body += g
    return svg(
        int(y) + 16,
        "What it covers",
        "Covered now, from CDC: notifiable infections by US state, from C. auris to syphilis; antibiotic "
        "resistance in hospital infections, by state; COVID-19, flu and RSV hospitalization rates at CDC "
        "surveillance sites; C. difficile infections, national, from CDC surveillance sites. From WHO: "
        "tuberculosis and new HIV infections by country. Coming next: measles cases by Michigan county; Detroit "
        "Health Department figures. Not covered yet: HIV in the United States.",
        body,
    )


# ---------------------------------------------------------------- 3. chart
# CDC NNDSS (data.cdc.gov/d/x9gk-5huc), Michigan, "Candida auris, clinical", checked 25 November 2026.
# year end: m3 (cumulative YTD) at the year's last MMWR week (2022-2024 week 52, 2025 week 53).
# later: m4 (previous year's YTD) in the next year's table at its last week; 2025's is 2026 week 45, the newest
# week published, so it is still rising. Every figure is provisional: CDC calls none of them final.
C_AURIS_MI: tuple[tuple[str, int, int], ...] = (
    ("2022", 27, 33),
    ("2023", 103, 110),
    ("2024", 174, 184),
    ("2025", 178, 217),
)


def chart(t: Theme) -> str:
    ink, muted, line, acc, soft = t["ink"], t["muted"], t["line"], t["accent"], t["accent_soft"]
    base, h, vmax, bw = 380, 224, 240, 48
    body = (
        f'<defs><pattern id="hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<rect width="8" height="8" fill="{soft}"/><rect width="3" height="8" fill="{acc}"/></pattern></defs>\n'
    )
    body += text(
        0, 36, 'Clinical <tspan font-style="italic">C. auris</tspan> cases, Michigan', ink, TITLE, 600
    )
    body += text(0, 76, "CDC yearly counts, all provisional", muted)
    slot = W / 4
    r = 4
    for i, (yr, end, later) in enumerate(C_AURIS_MI):
        cx = slot * i + slot / 2
        x0 = cx - bw / 2
        top = base - end / vmax * h
        ltop = base - later / vmax * h
        # The later count: a dashed outline from the axis to its height, behind the year-end bar.
        d_out = (
            f"M{x0} {base}V{ltop + r}Q{x0} {ltop} {x0 + r} {ltop}H{x0 + bw - r}"
            f"Q{x0 + bw} {ltop} {x0 + bw} {ltop + r}V{base}"
        )
        body += (
            f'<path d="{d_out}" fill="none" stroke="{acc}" stroke-width="2.5" stroke-dasharray="6 4" '
            f'stroke-linejoin="round"/>\n'
        )
        d = f"M{x0} {base}V{top + r}Q{x0} {top} {x0 + r} {top}H{x0 + bw - r}Q{x0 + bw} {top} {x0 + bw} {top + r}V{base}Z"
        body += f'<path d="{d}" fill="url(#hatch)"/>\n'
        label = f'<tspan fill="{muted}" font-weight="400" font-size="{BODY}">{end} \u2192 </tspan>{later}'
        body += text(cx, ltop - 12, label, ink, TITLE, 600, "middle")
        body += text(cx, base + 36, yr, muted, anchor="middle")
    body += f'<path d="M0 {base}H{W}" stroke="{line}" stroke-width="2"/>\n'
    ly = base + 92
    body += f'<rect x="0" y="{ly - 22}" width="28" height="28" rx="4" fill="url(#hatch)"/>\n'
    body += text(40, ly, "At year end", ink)
    body += (
        f'<rect x="260" y="{ly - 22}" width="28" height="28" rx="4" fill="none" stroke="{acc}" '
        f'stroke-width="2.5" stroke-dasharray="6 4"/>\n'
    )
    body += text(300, ly, "CDC\u2019s later count", ink)
    body += text(0, ly + 44, "2025\u2019s later count is to week 45 of 2026", muted)
    return svg(
        ly + 64,
        "Example answer: clinical C. auris cases in Michigan",
        "Bar chart of clinical Candida auris cases reported in Michigan, from CDC, checked 25 November 2026. "
        "All figures are provisional. Count at year end, then CDC's later count from the next year's table: "
        "2022, 27 then 33; 2023, 103 then 110; 2024, 174 then 184; 2025, 178 then 217 by week 45 of 2026.",
        body,
    )


def render() -> dict[str, str]:
    """Every README image, by file name."""
    out = {}
    for name, fn in [("how-it-works", hero), ("coverage", coverage), ("example-c-auris-michigan", chart)]:
        for theme, tok in THEMES.items():
            out[f"{name}-{theme}.svg"] = fn(tok)
    return out


def main(argv: list[str]) -> int:
    images = render()
    if "--check" in argv:
        stale = [n for n, s in images.items() if not (OUT / n).is_file() or (OUT / n).read_text("utf-8") != s]
        for n in stale:
            print(
                f"assets/readme/{n} differs from tools/readme_svg/gen.py; run python3 -I tools/readme_svg/gen.py"
            )
        return 1 if stale else 0
    OUT.mkdir(parents=True, exist_ok=True)
    for n, s in images.items():
        (OUT / n).write_text(s, encoding="utf-8")
    print(sorted(images))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
