"""Regenerate docs/helpers.md (the copy-paste helpers) from src/health_data_registry/helpers.py."""

from pathlib import Path

from health_data_registry.helpers import markdown

out = Path(__file__).resolve().parents[1] / "docs" / "helpers.md"
out.write_text(markdown(), encoding="utf-8")
print(f"wrote {out}")
