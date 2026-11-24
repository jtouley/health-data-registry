"""Write docs/helpers.md and the template's Claude Code skills (template/.claude/skills/<name>/SKILL.md).

Both come from the workflows in src/health_data_registry/helpers.py, the same records server.py serves as MCP
prompts. Skill folders that no longer match a workflow are removed. Run with `make docs`.
"""

import shutil
from pathlib import Path

from health_data_registry.helpers import SKILLS_DIR, generated_files

root = Path(__file__).resolve().parents[1]
files = generated_files()
skills = root / SKILLS_DIR
if skills.is_dir():
    keep = {Path(p).parent.name for p in files if p.startswith(SKILLS_DIR + "/")}
    for d in skills.iterdir():
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)
            print(f"removed {d.relative_to(root)}")
for rel, text in files.items():
    out = root / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"wrote {rel}")
