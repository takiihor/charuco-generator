import json
from pathlib import Path
import subprocess
import sys

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
    assert json.loads(good.stdout)["detected_marker_ids"] == list(range(10, 20))
    assert wrong.returncode == 1
