import json
import subprocess
import sys
from pathlib import Path

import cv2
import pytest

from charuco_generator import __version__
from charuco_generator.cli import main

ROOT = Path(__file__).resolve().parents[1]


def test_cli_generation_and_overrides(tmp_path, capsys):
    code = main(["generate", "--config", str(ROOT / "config/boards.json"),
                 "--squares-x", "4", "--squares-y", "4", "--square-size-mm", "18",
                 "--marker-size-mm", "12", "--dictionary", "DICT_4X4_100",
                 "--marker-ids", "front:20-27", "--marker-ids", "rear:30-37",
                 "--dpi", "300", "--page-size", "A3", "--white-margin-mm", "8",
                 "--page-margin-mm", "12", "--output-dir", str(tmp_path)])
    assert code == 0, capsys.readouterr()
    manifest = json.loads((tmp_path / "board_manifest.json").read_text())
    assert manifest["boards"][0]["marker_ids"] == list(range(20, 28))
    assert manifest["printing"]["page_size"] == "A3"
    assert manifest["boards"][0]["pattern_dimensions_mm"] == [72, 72]


def test_cli_validation_statuses(artifacts, capsys, tmp_path):
    directory, _, _ = artifacts
    common = ["--manifest", str(directory / "board_manifest.json"), "--input", str(directory / "front_charuco.png")]
    assert main(["validate", *common, "--board", "front", "--report", str(tmp_path / "report.json")]) == 0
    assert json.loads((tmp_path / "report.json").read_text())["mode"] == "synthetic"
    assert main(["validate", *common, "--board", "rear"]) == 1
    assert main(["validate", *common, "--board", "unknown"]) == 2
    assert main(["verify", "--manifest", str(directory / "board_manifest.json")]) == 0


def test_cli_invalid_overrides(tmp_path, capsys):
    assert main(["generate", "--marker-size-mm", "20", "--output-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())
    assert main(["generate", "--marker-ids", "0-9"]) == 2
    assert main(["generate", "--marker-ids", "front:19-10"]) == 2
    assert main(["generate", "--marker-ids", "unknown:0-9"]) == 2
    assert main(["generate", "--marker-ids", "front:abc"]) == 2
    assert main(["generate", "--board", "front", "--marker-ids", "0-9", "--marker-ids", "0-9"]) == 2


def test_cli_selection_and_automatic_ids(tmp_path, capsys):
    assert main(["generate", "--select", "rear", "--start-id", "40",
                 "--squares-x", "6", "--output-dir", str(tmp_path)]) == 0, capsys.readouterr()
    manifest = json.loads((tmp_path / "board_manifest.json").read_text())
    assert [board["name"] for board in manifest["boards"]] == ["rear"]
    assert manifest["boards"][0]["marker_ids"] == list(range(40, 52))
    assert not (tmp_path / "front_charuco.png").exists()


def test_cli_assigns_disjoint_ranges_to_all_boards(tmp_path, capsys):
    assert main(["generate", "--start-id", "60", "--output-dir", str(tmp_path)]) == 0, capsys.readouterr()
    manifest = json.loads((tmp_path / "board_manifest.json").read_text())
    assert [board["marker_ids"] for board in manifest["boards"]] == [list(range(60, 70)), list(range(70, 80))]


def test_cli_rejects_invalid_selection_and_id_capacity(tmp_path):
    for options in (["--select", "missing"], ["--select", "front", "front"],
                    ["--start-id", "-1"], ["--start-id", "81"]):
        assert main(["generate", *options, "--output-dir", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())


def test_standalone_entrypoint_exit_codes(artifacts):
    directory, _, _ = artifacts
    args = [sys.executable, str(ROOT / "generate.py"), "validate", "--manifest", str(directory / "board_manifest.json"),
            "--input", str(directory / "rear_charuco.png"), "--board"]
    good = subprocess.run([*args, "rear"], cwd=ROOT, capture_output=True, text=True)
    wrong = subprocess.run([*args, "front"], cwd=ROOT, capture_output=True, text=True)
    assert good.returncode == 0
    assert json.loads(good.stdout)["detected_marker_ids"] == list(range(11, 21))
    assert wrong.returncode == 1


def test_untested_opencv_is_refused_then_warns(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHARUCO_ALLOW_UNTESTED_OPENCV", "")
    monkeypatch.setattr(cv2, "__version__", "4.5.0")
    target = tmp_path / "untested"
    assert main(["generate", "--config", str(ROOT / "config/boards.json"), "--output-dir", str(target)]) == 2
    assert "Tested OpenCV 4.11.0" in capsys.readouterr().err
    assert not target.exists()
    assert main(["generate", "--config", str(ROOT / "config/boards.json"),
                 "--output-dir", str(target), "--allow-untested-opencv"]) == 0
    report = json.loads((target / "validation_report.json").read_text())
    assert report["passed"]
    assert any("not the tested 4.11.0" in warning for warning in report["warnings"])


def test_tile_overlap_flag_splits_an_oversized_board(tmp_path, capsys):
    target = tmp_path / "tiled"
    assert main(["generate", "--config", str(ROOT / "config/boards.json"), "--select", "front",
                 "--squares-x", "10", "--squares-y", "8", "--square-size-mm", "30", "--marker-size-mm", "21",
                 "--start-id", "0", "--dpi", "150", "--output-dir", str(target)]) == 2
    error = capsys.readouterr().err
    assert "--tile-overlap-mm" in error and "Automatic scaling is forbidden" in error
    assert main(["generate", "--config", str(ROOT / "config/boards.json"), "--select", "front",
                 "--squares-x", "10", "--squares-y", "8", "--square-size-mm", "30", "--marker-size-mm", "21",
                 "--start-id", "0", "--dpi", "150", "--tile-overlap-mm", "12", "--output-dir", str(target)]) == 0
    manifest = json.loads((target / "board_manifest.json").read_text())
    assert len(manifest["pdf_layouts"]["front_charuco.pdf"]) > 1
    assert main(["verify", "--manifest", str(target / "board_manifest.json")]) == 0


def test_detector_flags_only_accept_valid_ranges(artifacts, tmp_path, capsys):
    directory, _, _ = artifacts
    common = ["validate", "--manifest", str(directory / "board_manifest.json"),
              "--input", str(directory / "front_charuco.png"), "--board", "front"]
    assert main([*common, "--corner-refinement", "subpix", "--min-perimeter-rate", "0.01"]) == 0
    assert main([*common, "--min-perimeter-rate", "9"]) == 2
    assert "min_perimeter_rate" in capsys.readouterr().err


def test_version_reports_the_package_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"charuco-generator {__version__}"


def _settings(manifest: dict) -> dict:
    """The saved configuration without its destination path, which is where each run writes."""
    return {key: value for key, value in manifest["configuration"].items() if key != "output_dir"}


def test_generate_reproduces_a_saved_revision(artifacts, tmp_path, capsys):
    directory, manifest, _ = artifacts
    again = tmp_path / "reprint"
    assert main(["generate", "--from-manifest", str(directory / "board_manifest.json"),
                 "--output-dir", str(again)]) == 0, capsys.readouterr()
    reproduced = json.loads((again / "board_manifest.json").read_text())
    assert _settings(reproduced) == _settings(manifest)
    assert (again / "front_charuco.png").read_bytes() == (directory / "front_charuco.png").read_bytes()


def test_from_manifest_without_output_dir_reprints_in_place(tmp_path, capsys):
    # A private set: reprints rewrite the manifest's recorded destination, so the shared fixture stays untouched.
    directory = tmp_path / "set"
    assert main(["generate", "--output-dir", str(directory)]) == 0, capsys.readouterr()
    before = (directory / "board_manifest.json").read_text()
    assert main(["generate", "--from-manifest", str(directory / "board_manifest.json")]) == 0, capsys.readouterr()
    rewritten = json.loads((directory / "board_manifest.json").read_text())
    assert (directory / "board_manifest.json").read_text() == before
    assert rewritten["configuration"]["output_dir"] == str(directory)


def test_from_manifest_rejects_an_aruco_manifest(tmp_path, capsys):
    source = tmp_path / "markers"
    assert main(["aruco", "--ids", "1-2", "--output-dir", str(source)]) == 0, capsys.readouterr()
    assert main(["generate", "--from-manifest", str(source / "aruco_manifest.json"),
                 "--output-dir", str(tmp_path / "boards")]) == 2
    assert "aruco --ids" in capsys.readouterr().err


@pytest.mark.parametrize("document,expected", [
    ('{"schema_version": 1}', "missing required key(s)"),
    ("not json", "Invalid JSON"),
    ("[1, 2]", "must contain a JSON object"),
])
def test_malformed_manifest_is_named_not_opaque(artifacts, tmp_path, capsys, document, expected):
    directory, _, _ = artifacts
    path = tmp_path / "broken_manifest.json"
    path.write_text(document)
    assert main(["verify", "--manifest", str(path)]) == 2
    error = capsys.readouterr().err
    assert str(path) in error and expected in error
    # Failing to read some other file must not disturb the real artifacts.
    assert main(["verify", "--manifest", str(directory / "board_manifest.json")]) == 0
