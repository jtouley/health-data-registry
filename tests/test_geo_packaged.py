"""geo outside the repo checkout (#223): the place list (contracts/models/dim_place.odcs.yaml) travels in the wheel
with every other contract, and is read on first use, never at import. Builds the wheel and runs it from a temporary
directory, so the checkout's own contracts/ can't stand in for the packaged copy."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

PROBE = """
import sys
sys.path.insert(0, sys.argv[1])
import health_data_registry.registry  # what every fuzz target and adapter imports first
from health_data_registry import geo
assert geo.__file__.startswith(sys.argv[1]), geo.__file__
if sys.argv[2] == "full":
    assert geo.key_for("Michigan") == "US-26" and geo.key_for("Detroit") == "US-26163/detroit"
    assert geo.places()[0].name == "United States" and "WHO-AFR" in geo.fixed_keys()
    print("resolved")
else:
    try:
        geo.key_for("Michigan")
    except RuntimeError as e:
        assert "dim_place.odcs.yaml" in str(e)
        print("refused on use")
"""


def _exec(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a command with fixed arguments (no shell) and return its exit code, stdout and stderr."""

    async def run() -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(), err.decode()

    return asyncio.run(run())


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    assert uv is not None, "the wheel is built with uv, as `make install` and CI do"
    out = tmp_path_factory.mktemp("wheel")
    code, _out, err = _exec([uv, "build", "--wheel", "--out-dir", str(out), "-q"], REPO)
    assert code == 0, err
    (whl,) = out.glob("*.whl")
    return whl


def _run(target: Path, mode: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "HDR_REGISTRY"}
    code, out, err = _exec([sys.executable, "-c", PROBE, str(target), mode], target, env)
    assert code == 0, err
    return out.strip()


def test_the_wheel_carries_the_place_list_and_geo_resolves_from_it(wheel: Path, tmp_path: Path) -> None:
    with zipfile.ZipFile(wheel) as z:
        assert "health_data_registry/_registry/contracts/models/dim_place.odcs.yaml" in z.namelist()
        z.extractall(tmp_path / "site")
    assert _run(tmp_path / "site", "full") == "resolved"


def test_importing_geo_needs_no_contract_until_a_place_is_asked_for(wheel: Path, tmp_path: Path) -> None:
    """A build without the contracts (a fuzz target's PyInstaller bundle) still imports; only a place lookup
    needs the list, and it says plainly where the list should be."""
    with zipfile.ZipFile(wheel) as z:
        z.extractall(tmp_path / "bare", members=[n for n in z.namelist() if "/_registry/" not in n])
    assert _run(tmp_path / "bare", "bare") == "refused on use"
