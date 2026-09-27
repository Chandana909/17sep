"""Where configuration lives: `$ASAS_CONFIG_DIR`, else the repository's `config/` folder.

Every config file (asas.toml, business_context.toml, mapping.synonyms.toml, rulesets/) is
resolved from this one directory, so a container or an installed package mounts its
configuration in one place.
"""

from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    override = os.environ.get("ASAS_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "config"
