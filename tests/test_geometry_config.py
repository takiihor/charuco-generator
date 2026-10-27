import json
from copy import deepcopy

import cv2
import numpy as np
import pytest

from charuco_generator.board import create_board, reconstruct_board
from charuco_generator.config import (
    ConfigError,
    config_from_dict,
    load_config,
    page_size_mm,
)


def test_default_dimensions_and_internal_metres(config):
    board = create_board(config.geometry, config.boards[0])
    assert config.geometry.pattern_mm == (100.0, 80.0)
    assert board.getChessboardSize() == (5, 4)
    assert board.getSquareLength() == pytest.approx(0.02)
    assert board.getMarkerLength() == pytest.approx(0.014)
    np.testing.assert_allclose(board.getRightBottomCorner(), [0.1, 0.08, 0], atol=1e-8)
    expected = [[x * 0.02, y * 0.02, 0] for y in range(1, 4) for x in range(1, 5)]
    np.testing.assert_allclose(board.getChessboardCorners(), expected, atol=1e-8)


def test_ids_and_placement(config):
    front, rear = (create_board(config.geometry, b) for b in config.boards)
    assert front.getIds().tolist() == list(range(1, 11))
    assert rear.getIds().tolist() == list(range(11, 21))
    assert not set(front.getIds()) & set(rear.getIds())
    assert len(front.getObjPoints()) == 10
    # IDs are assigned row-major to white cells: first marker is in column 1, row 0.
    np.testing.assert_allclose(front.getObjPoints()[0][0], [0.023, 0.003, 0], atol=1e-8)
    np.testing.assert_allclose(rear.getObjPoints()[0], front.getObjPoints()[0])


def test_json_round_trip(config, tmp_path):
    path = tmp_path / "boards.json"
    path.write_text(json.dumps(config.to_dict()))
    assert load_config(path) == config


@pytest.mark.parametrize("section,key,value", [
    ("geometry", "squares_x", 1), ("geometry", "squares_y", 0),
    ("geometry", "squares_x", 5.0), ("geometry", "squares_y", True),
    ("geometry", "square_length_mm", 0), ("geometry", "square_length_mm", float("nan")),
    ("geometry", "marker_length_mm", 20), ("geometry", "marker_length_mm", 21),
    ("geometry", "marker_length_mm", -1), ("geometry", "square_length_mm", "20"),
    ("geometry", "dictionary", "DICT_NO_SUCH_DICTIONARY"),
    ("geometry", "legacy_pattern", "false"), ("geometry", "border_bits", 0),
    ("printing", "dpi", 0), ("printing", "dpi", 600.5),
    ("printing", "white_margin_mm", 0), ("printing", "white_margin_mm", 2.9),
    ("printing", "page_margin_mm", -1), ("printing", "page_size", "banana"),
    ("printing", "page_size", "0x297"), ("printing", "unexpected_typo", 12),
])
def test_invalid_parameters_rejected(config, section, key, value):
    data = config.to_dict()
    data[section][key] = value
    with pytest.raises(ConfigError):
        config_from_dict(data)


@pytest.mark.parametrize("case", [
    "within_board", "across_boards", "count", "negative", "capacity", "bool", "name",
    "identifier", "schema", "root", "unknown", "boards", "ids",
])
def test_invalid_board_configuration(config, case):
    data = config.to_dict()
    if case == "within_board":
        data["boards"][0]["marker_ids"][1] = data["boards"][0]["marker_ids"][0]
    elif case == "across_boards":
        data["boards"][1]["marker_ids"][0] = data["boards"][0]["marker_ids"][0]
    elif case == "count":
        data["boards"][0]["marker_ids"].pop()
    elif case in ("negative", "capacity", "bool"):
        data["boards"][0]["marker_ids"][0] = {"negative": -1, "capacity": 100, "bool": True}[case]
    elif case == "name":
        data["boards"][0]["name"] = "../../unsafe"
    elif case == "identifier":
        data["boards"][1]["identifier"] = data["boards"][0]["identifier"]
    elif case == "schema":
        data["schema_version"] = 2
    elif case == "root":
        data = []
    elif case == "unknown":
        data["unkown"] = 123
    elif case == "boards":
        data["boards"] = []
    elif case == "ids":
        data["boards"][0]["marker_ids"] = "0-9"
    with pytest.raises(ConfigError):
        config_from_dict(data)


def test_invalid_json_report(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{")
    with pytest.raises(ConfigError, match="Invalid JSON"):
        load_config(path)


def test_page_sizes():
    assert page_size_mm("a4") == (210, 297)
    assert page_size_mm("LETTER") == (215.9, 279.4)
    assert page_size_mm("300x400") == (300, 400)


def test_manifest_reconstruction(config, artifacts):
    _, manifest, _ = artifacts
    for spec in config.boards:
        original = create_board(config.geometry, spec)
        rebuilt = reconstruct_board(manifest, spec.name)
        np.testing.assert_array_equal(original.getIds(), rebuilt.getIds())
        np.testing.assert_array_equal(original.getChessboardCorners(), rebuilt.getChessboardCorners())
        np.testing.assert_array_equal(original.getObjPoints(), rebuilt.getObjPoints())
        np.testing.assert_array_equal(original.generateImage((1000, 800)), rebuilt.generateImage((1000, 800)))
        assert rebuilt.getLegacyPattern() is False


@pytest.mark.parametrize("field", ["configuration", "placement", "version", "unknown", "corners"])
def test_inconsistent_manifest_rejected(artifacts, field):
    _, manifest, _ = artifacts
    manifest = deepcopy(manifest)
    name = "front"
    if field == "configuration":
        manifest["configuration"]["geometry"]["square_length_mm"] = 21
    elif field == "placement":
        manifest["boards"][0]["marker_ids"].reverse()
    elif field == "version":
        manifest["environment"]["opencv"] = "4.5.0"
    elif field == "unknown":
        name = "unknown"
    elif field == "corners":
        manifest["boards"][0]["charuco_corners"][0]["point_m"][0] = 999
    with pytest.raises(ConfigError):
        reconstruct_board(manifest, name)


def test_unsupported_opencv_rejected(config, monkeypatch):
    monkeypatch.setattr(cv2, "__version__", "4.5.0")
    with pytest.raises(ConfigError, match=r"4\.11\.0"):
        create_board(config.geometry, config.boards[0])
