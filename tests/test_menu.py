import json
from pathlib import Path

import pytest

from charuco_generator.cli import main
from charuco_generator.config import ConfigError, config_from_dict
from charuco_generator.menu import configure
from charuco_generator.selection import assign_ids, ids_label, next_ids

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/boards.json"


def answers(monkeypatch, values):
    responses = iter(values)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))


def test_menu_generates_new_sets_without_overwriting(monkeypatch, tmp_path):
    original = CONFIG_PATH.read_bytes()
    for _ in range(2):
        answers(monkeypatch, ["", "", "1", ""])
        assert main(["menu", "--config", str(CONFIG_PATH), "--output-dir", str(tmp_path)]) == 0
    manifests = sorted(tmp_path.rglob("board_manifest.json"))
    assert len(manifests) == 2
    encoded = []
    for path in manifests:
        manifest = json.loads(path.read_text())
        encoded.append({marker for board in manifest["boards"] for marker in board["marker_ids"]})
        assert (path.parent / manifest["combined_pdf"]).is_file()
        assert main(["verify", "--manifest", str(path)]) == 0
    assert encoded == [set(range(21, 41)), set(range(41, 61))]
    assert CONFIG_PATH.read_bytes() == original


def test_menu_selects_only_rear_and_keeps_ids(monkeypatch, tmp_path):
    answers(monkeypatch, ["invalid", "2", "", "", "", "", "", "", "", "2", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert [board.name for board in config.boards] == ["rear"]
    assert config.boards[0].marker_ids == tuple(range(11, 21))
    assert not tmp_path.joinpath("rear_10-19").exists()


def test_menu_custom_geometry_recomputes_id_count(monkeypatch, tmp_path):
    answers(monkeypatch, ["all", "6x4", "20", "14", "DICT_5X5_100", "A4", "300", "",
                          "1", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert config.geometry.squares_x == 6
    assert config.boards[0].marker_ids == tuple(range(21, 33))
    assert config.boards[1].marker_ids == tuple(range(33, 45))


def test_menu_manual_ids_and_cancel(monkeypatch, tmp_path):
    answers(monkeypatch, ["front", "", "", "", "", "", "", "", "3", "50", "", "n"])
    assert main(["menu", "--advanced", "--output-dir", str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())


def test_simple_menu_keeps_configured_ids_by_default(monkeypatch, tmp_path):
    answers(monkeypatch, ["front", "", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path))
    assert config.boards[0].marker_ids == tuple(range(1, 11))
    assert config.boards[0].identifier == "front-support-v2"


def test_simple_menu_id_count_mismatch_retries(monkeypatch, tmp_path, capsys):
    answers(monkeypatch, ["front", "30x30", "", "3", "1", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path))
    assert config.boards[0].marker_ids == (1, 2, 3, 4)
    assert "needs 4 markers per board, but 10 IDs are listed" in capsys.readouterr().out


def test_simple_menu_generates_30mm_square_pattern(monkeypatch, tmp_path, capsys):
    prompts = []
    responses = iter(["all", "30x30", "1", "y"])

    def respond(prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", respond)
    assert main(["menu", "--output-dir", str(tmp_path)]) == 0, capsys.readouterr()
    assert len(prompts) == 4
    path, = tmp_path.rglob("board_manifest.json")
    manifest = json.loads(path.read_text())
    geometry = manifest["configuration"]["geometry"]
    assert (geometry["squares_x"], geometry["squares_y"]) == (3, 3)
    assert geometry["square_length_mm"] == 10
    assert geometry["marker_length_mm"] == 7
    assert [b["pattern_dimensions_mm"] for b in manifest["boards"]] == [[30, 30], [30, 30]]
    assert [b["marker_ids"] for b in manifest["boards"]] == [list(range(21, 25)), list(range(25, 29))]


def test_simple_menu_retries_only_pattern_size(monkeypatch, tmp_path, capsys):
    answers(monkeypatch, ["front", "invalid", "0x30", "3x3", "300x300", "90x90", "1", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path))
    assert config.geometry.pattern_mm == (90, 90)
    assert config.geometry.marker_length_mm == 21
    assert not list(tmp_path.iterdir())
    output = capsys.readouterr().out
    assert "Enter width x height" in output
    assert "Width and height must be greater than zero" in output
    assert "Raster marker resolution too low" in output
    assert "exceed printable page area" in output


def test_simple_menu_resizes_rectangular_pattern(monkeypatch, tmp_path):
    answers(monkeypatch, ["front", "120 × 80 mm", "1", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path))
    assert config.geometry.pattern_mm == (120, 80)
    assert (config.geometry.squares_x, config.geometry.squares_y) == (6, 4)
    assert len(config.boards[0].marker_ids) == 12


def test_advanced_menu_retries_invalid_fields_immediately(monkeypatch, tmp_path, capsys):
    answers(monkeypatch, ["front", "30", "3x3", "30", "30", "21", "30", "DICT_5X5_100",
                          "bad", "a4", "bad", "300", "150", "0", "1", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert config.geometry.pattern_mm == (90, 90)
    assert config.printing.page_size == "A4"
    assert config.printing.dpi == 300
    assert config.printing.tile_overlap_mm == 0
    output = capsys.readouterr().out
    assert "Marker size must be smaller than the 30 mm square" in output
    assert "from 0 to 100" in output


def test_advanced_menu_accepts_tile_overlap(monkeypatch, tmp_path):
    answers(monkeypatch, ["front", "", "", "", "", "", "", "8", "2", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert config.printing.tile_overlap_mm == 8


@pytest.mark.parametrize("exception", [KeyboardInterrupt, EOFError])
def test_menu_interruption_is_clean(monkeypatch, tmp_path, exception):
    def interrupt(_):
        raise exception
    monkeypatch.setattr("builtins.input", interrupt)
    assert main(["menu", "--output-dir", str(tmp_path)]) == 130
    assert not list(tmp_path.iterdir())


def test_next_ids_respects_dictionary_and_saved_nonconsecutive_ids(config, tmp_path):
    saved = config.to_dict()
    assign_ids(saved, 20)
    saved["boards"][0]["marker_ids"] = [*list(range(20, 29)), 45]
    tmp_path.joinpath("board_manifest.json").write_text(json.dumps({"configuration": saved}))
    assert next_ids(config, config.to_dict(), tmp_path) == 46
    saved["geometry"]["dictionary"] = "DICT_4X4_100"
    tmp_path.joinpath("board_manifest.json").write_text(json.dumps({"configuration": saved}))
    assert next_ids(config, config.to_dict(), tmp_path) == 21


def test_next_ids_fails_on_corrupt_manifest(config, tmp_path):
    tmp_path.joinpath("board_manifest.json").write_text("{")
    with pytest.raises(ConfigError, match="could not be read") as excinfo:
        next_ids(config, config.to_dict(), tmp_path)
    assert "--ignore-unreadable-manifests" in str(excinfo.value)
    assert "board_manifest.json" in str(excinfo.value)


def test_next_ids_can_ignore_a_corrupt_manifest(config, tmp_path, capsys):
    tmp_path.joinpath("board_manifest.json").write_text("{")
    warnings = []
    assert next_ids(config, config.to_dict(), tmp_path, ignore_unreadable=True, warnings=warnings) == 21
    assert "unreadable manifest" in warnings[0]


def test_next_ids_reserves_ids_from_aruco_manifests(config, tmp_path):
    (tmp_path / "markers").mkdir()
    (tmp_path / "markers" / "aruco_manifest.json").write_text(json.dumps(
        {"kind": "aruco", "marker_ids": list(range(21, 41)), "configuration": {"dictionary": "DICT_5X5_100"}}))
    assert next_ids(config, config.to_dict(), tmp_path) == 41
    # A manifest recorded against a different dictionary must not block this one.
    (tmp_path / "markers" / "aruco_manifest.json").write_text(json.dumps(
        {"kind": "aruco", "marker_ids": list(range(21, 41)), "configuration": {"dictionary": "DICT_4X4_50"}}))
    assert next_ids(config, config.to_dict(), tmp_path) == 21


@pytest.mark.parametrize("ids, label", [
    ((), "no-ids"), ((7,), "7"), ((1, 2, 3), "1-3"), ((3, 1, 2), "1-3"),
    ((1, 2, 5, 9, 10), "1-2+5+9-10"),
])
def test_ids_label_describes_nonconsecutive_sets(ids, label):
    assert ids_label(ids) == label


def test_next_ids_exhaustion(config, tmp_path):
    for start in range(20, 100, 20):
        saved = config.to_dict()
        assign_ids(saved, start)
        folder = tmp_path / str(start)
        folder.mkdir()
        (folder / "board_manifest.json").write_text(json.dumps({"configuration": saved}))
    with pytest.raises(ConfigError, match="No unused block"):
        next_ids(config, config.to_dict(), tmp_path)


def test_auto_ids_update_reference_identity(config):
    first, second = config.to_dict(), config.to_dict()
    assign_ids(first, 20)
    assign_ids(second, 40)
    assert first["boards"][0]["identifier"] != second["boards"][0]["identifier"]
    assert config_from_dict(first).boards[0].identifier != config.boards[0].identifier


def test_nonobject_saved_manifest_refuses_automatic_id_allocation(config, tmp_path):
    (tmp_path / 'board_manifest.json').write_text('[]')
    with pytest.raises(ConfigError, match='could not be read'):
        next_ids(config, config.to_dict(), tmp_path)
