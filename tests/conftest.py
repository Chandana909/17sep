from __future__ import annotations

from pathlib import Path

import pytest

from asas.core.config import Config, default_config_path, load_config
from asas.data.synthetic import GeneratorSpec, SyntheticDataset, generate
from asas.services.demo import DemoResult, run_demo


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(default_config_path())


@pytest.fixture(scope="session")
def dataset() -> SyntheticDataset:
    return generate(GeneratorSpec())


@pytest.fixture(scope="session")
def demo(tmp_path_factory: pytest.TempPathFactory, cfg: Config) -> DemoResult:
    """The full lifecycle, run once per test session (about 20 seconds)."""
    return run_demo(tmp_path_factory.mktemp("demo") / "asas.db", cfg)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "asas.db"
