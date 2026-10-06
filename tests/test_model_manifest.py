from __future__ import annotations

import pytest

from scripts.model_manifest import inventory


def test_offline_inventory_detects_content_changes_and_symlinks(tmp_path) -> None:
    model = tmp_path / "model.bin"
    model.write_bytes(b"approved")
    first = inventory(tmp_path)
    model.write_bytes(b"tampered")
    assert inventory(tmp_path) != first
    (tmp_path / "link.bin").symlink_to(model)
    with pytest.raises(ValueError, match="Символическая"):
        inventory(tmp_path)
