from pathlib import Path

import pytest

from charuco_generator.config import load_config
from charuco_generator.generator import generate
from charuco_generator.validation import load_manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def config():
    return load_config(ROOT / "config/boards.json")


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory, config):
    directory = tmp_path_factory.mktemp("boards")
    report = generate(config, directory)
    return directory, load_manifest(directory / "board_manifest.json"), report
