"""The private tier names its Hugging Face datasets through naming.py (docs/dev/naming.md)."""

from __future__ import annotations

import pytest

from health_data_registry import naming, private


def test_private_tier_uses_the_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(naming.OWNER_ENV, raising=False)
    assert private.hf_repo("croi_abstracts_index") is None
    assert (
        private.hf_repo("croi_abstracts_index", "org")
        == "org/health-data-registry-private-croi-abstracts-index"
    )
    monkeypatch.setenv(naming.OWNER_ENV, "jtouley")
    assert (
        private.hf_repo("croi_abstracts_text") == "jtouley/health-data-registry-private-croi-abstracts-text"
    )
    with pytest.raises(private.PrivateRefused):
        private.hf_repo("Bad-Id", "org")
