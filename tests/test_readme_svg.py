"""The README images in assets/readme/ are exactly what tools/readme_svg/gen.py draws (edit the generator, not the SVGs)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _gen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("readme_svg_gen", ROOT / "tools" / "readme_svg" / "gen.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_svgs_match_the_generator() -> None:
    images = _gen().render()
    committed = sorted(p.name for p in (ROOT / "assets" / "readme").glob("*.svg"))
    assert committed == sorted(images), "an SVG in assets/readme/ has no generator, or one wasn't written"
    for name, svg in images.items():
        assert (ROOT / "assets" / "readme" / name).read_text(encoding="utf-8") == svg, (
            f"assets/readme/{name} differs from tools/readme_svg/gen.py; run python3 -I tools/readme_svg/gen.py"
        )


def test_every_c_auris_year_is_provisional_with_a_later_count() -> None:
    gen = _gen()
    for _year, at_year_end, later in gen.C_AURIS_MI:
        assert later >= at_year_end
    chart = gen.render()["example-c-auris-michigan-light.svg"]
    assert "all provisional" in chart
    for year, at_year_end, later in gen.C_AURIS_MI:
        assert f"{year}, {at_year_end} then {later}" in chart


def test_check_mode_reports_no_drift() -> None:
    assert _gen().main(["--check"]) == 0
