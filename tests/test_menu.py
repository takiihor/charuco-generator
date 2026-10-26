import json
from pathlib import Path

import pytest

from charuco_generator.cli import main
from charuco_generator.config import ConfigError, config_from_dict
from charuco_generator.menu import configure
from charuco_generator.selection import assign_ids, next_ids

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config/boards.json"


def answers(monkeypatch, values):
    responses = iter(values)
    monkeypatch.setattr("builtins.input", lambda _: next(responses))


def test_menu_generates_new_sets_without_overwriting(monkeypatch, tmp_path):
    original = CONFIG_PATH.read_bytes()
    for _ in range(2):
        answers(monkeypatch, [""] * 3)
        assert main(["menu", "--config", str(CONFIG_PATH), "--output-dir", str(tmp_path)]) == 0
    manifests = sorted(tmp_path.rglob("board_manifest.json"))
    assert len(manifests) == 2
    encoded = []
    for path in manifests:
        manifest = json.loads(path.read_text())
        encoded.append({marker for board in manifest["boards"] for marker in board["marker_ids"]})
        assert (path.parent / manifest["combined_pdf"]).is_file()
        assert main(["verify", "--manifest", str(path)]) == 0
    assert encoded == [set(range(20, 40)), set(range(40, 60))]
    assert CONFIG_PATH.read_bytes() == original


def test_menu_selects_only_rear_and_keeps_ids(monkeypatch, tmp_path):
    answers(monkeypatch, ["invalid", "2", "", "", "", "", "", "", "2", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert [board.name for board in config.boards] == ["rear"]
    assert config.boards[0].marker_ids == tuple(range(10, 20))
    assert not tmp_path.joinpath("rear_10-19").exists()


def test_menu_custom_geometry_recomputes_id_count(monkeypatch, tmp_path):
    answers(monkeypatch, ["all", "6x4", "20", "14", "DICT_5X5_100", "A4", "300",
                          "1", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert config.geometry.squares_x == 6
    assert config.boards[0].marker_ids == tuple(range(20, 32))
    assert config.boards[1].marker_ids == tuple(range(32, 44))


def test_menu_manual_ids_and_cancel(monkeypatch, tmp_path):
    answers(monkeypatch, ["front", "", "", "", "", "", "", "3", "50", "", "n"])
    assert main(["menu", "--advanced", "--output-dir", str(tmp_path)]) == 0
    assert not list(tmp_path.iterdir())


def test_simple_menu_generates_30mm_square_pattern(monkeypatch, tmp_path, capsys):
    prompts = []
    responses = iter(["all", "30x30", "y"])

    def respond(prompt):
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", respond)
    assert main(["menu", "--output-dir", str(tmp_path)]) == 0, capsys.readouterr()
    assert len(prompts) == 3
    path, = tmp_path.rglob("board_manifest.json")
    manifest = json.loads(path.read_text())
    geometry = manifest["configuration"]["geometry"]
    assert (geometry["squares_x"], geometry["squares_y"]) == (3, 3)
    assert geometry["square_length_mm"] == 10
    assert geometry["marker_length_mm"] == 7
    assert [b["pattern_dimensions_mm"] for b in manifest["boards"]] == [[30, 30], [30, 30]]
    assert [b["marker_ids"] for b in manifest["boards"]] == [list(range(20, 24)), list(range(24, 28))]


def test_simple_menu_retries_only_pattern_size(monkeypatch, tmp_path, capsys):
    answers(monkeypatch, ["front", "invalid", "0x30", "3x3", "300x300", "90x90", "y"])
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
    answers(monkeypatch, ["front", "120 × 80 mm", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path))
    assert config.geometry.pattern_mm == (120, 80)
    assert (config.geometry.squares_x, config.geometry.squares_y) == (6, 4)
    assert len(config.boards[0].marker_ids) == 12


def test_advanced_menu_retries_invalid_fields_immediately(monkeypatch, tmp_path, capsys):
    answers(monkeypatch, ["front", "30", "3x3", "30", "30", "21", "30", "DICT_5X5_100",
                          "bad", "a4", "bad", "300", "1", "", "y"])
    config = configure(str(CONFIG_PATH), str(tmp_path), advanced=True)
    assert config.geometry.pattern_mm == (90, 90)
    assert config.printing.page_size == "A4"
    assert config.printing.dpi == 300
    assert "Marker size must be smaller than the 30 mm square" in capsys.readouterr().out


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
    saved["boards"][0]["marker_ids"] = list(range(20, 29)) + [45]
    tmp_path.joinpath("board_manifest.json").write_text(json.dumps({"configuration": saved}))
    assert next_ids(config, config.to_dict(), tmp_path) == 46
    saved["geometry"]["dictionary"] = "DICT_4X4_100"
    tmp_path.joinpath("board_manifest.json").write_text(json.dumps({"configuration": saved}))
    assert next_ids(config, config.to_dict(), tmp_path) == 20


def test_next_ids_fails_on_corrupt_manifest(config, tmp_path):
    tmp_path.joinpath("board_manifest.json").write_text("{")
    with pytest.raises(ConfigError, match="Cannot check saved IDs"):
        next_ids(config, config.to_dict(), tmp_path)


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
