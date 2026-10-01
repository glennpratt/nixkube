# SPDX-License-Identifier: MIT
"""Fixtures every test gets."""

import pytest

from src.nri import resolution


@pytest.fixture(autouse=True)
def _own_resolutions(tmp_path, monkeypatch):
    """Each test resolves into its own record, never the node's real one
    (`CSI_ROOT`), and never into another test's."""
    monkeypatch.setattr(
        resolution,
        "_resolutions",
        resolution.Resolutions(tmp_path / "resolutions.json"),
    )
