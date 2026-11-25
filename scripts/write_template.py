"""Write every file generated from the workflows in src/health_data_registry/helpers.py.

`make docs` (no arguments) writes the files kept in git: docs/helpers.md, the template's Claude Code skills
(template/.claude/skills/<name>/SKILL.md), the plugin (plugin/), its marketplace (.claude-plugin/marketplace.json)
and the tool lists in README.md, docs/tools.md and CLAUDE.md.
Skill folders that no longer match a workflow are removed.

`make kit` (--kit DIR) builds the Claude Projects kit into DIR (default dist/projects-kit, ignored by git): the
Project instructions, the knowledge files and one upload zip per skill. DIR is emptied first.
"""

import argparse
import shutil
from pathlib import Path

from health_data_registry import distribution
from health_data_registry.helpers import SKILLS, SKILLS_DIR, generated_files

root = Path(__file__).resolve().parents[1]


def prune(skills_dir: Path) -> None:
    keep = {w.name for w in SKILLS}
    if skills_dir.is_dir():
        for d in skills_dir.iterdir():
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d)
                print(f"removed {d.relative_to(root)}")


def write_docs() -> None:
    prune(root / SKILLS_DIR)
    prune(root / distribution.PLUGIN_DIR / "skills")
    for rel, text in {**generated_files(), **distribution.plugin_files(root)}.items():
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {rel}")
    table = distribution.tool_table()
    for rel in distribution.TOOL_LIST_FILES:
        path = root / rel
        text = path.read_text(encoding="utf-8")
        new = distribution.with_tool_table(text, table)
        if new != text:
            path.write_text(new, encoding="utf-8")
            print(f"wrote the tool list in {rel}")


def write_kit(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    for rel, data in distribution.kit_files(root).items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"wrote {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kit", metavar="DIR", type=Path, help="build the Claude Projects kit into DIR")
    args = parser.parse_args()
    if args.kit:
        write_kit(args.kit if args.kit.is_absolute() else root / args.kit)
    else:
        write_docs()
