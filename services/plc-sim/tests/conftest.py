"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from plc_sim.config import load_config

TAGS_FILE = Path(__file__).resolve().parents[1] / "config" / "tags.yaml"


@pytest.fixture(scope="session")
def tags_file() -> Path:
    return TAGS_FILE


@pytest.fixture
def cfg():
    return load_config(TAGS_FILE)
