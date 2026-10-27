import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import pytest

from charuco_generator.cli import main
from charuco_generator.config import ConfigError, dictionary_for
from charuco_generator.generator import generate
from charuco_generator.sheet import combined_pdf_name
from charuco_generator.tanks import MIN_CORNERS, TankSetTarget, tank_config, tank_for_marker_id, tank_table
from charuco_generator.validation import load_manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def tank_set(tmp_path_factory):
    directory = tmp_path_factory.mktemp("tanks")
    config = tank_config(TankSetTarget(dpi=200), str(directory))
    report = generate(config)
    return directory, config, report


def test_default_set_is_twenty_trusted_boards_with_disjoint_id_blocks(tank_set):
    _, config, report = tank_set
    table = tank_table(config)
    assert [row["tank"] for row in table] == list(range(1, 21))
    assert [row["board"] for row in table] == [f"tank{n:02d}" for n in range(1, 21)]
    assert [row["marker_ids"] for row in table] == [list(range(17 + 8 * i, 25 + 8 * i)) for i in range(20)]
    geometry = config.geometry
    assert (geometry.dictionary, geometry.squares_x, geometry.squares_y) == ("DICT_6X6_250", 4, 4)
    assert geometry.pattern_mm == (80.0, 80.0) and geometry.corner_count >= MIN_CORNERS
    assert report["passed"]
    assert all((b["detected_marker_count"], b["detected_charuco_corner_count"]) == (8, 9) for b in report["boards"])


def test_ids_never_collide_with_calibration_or_front_rear():
    ids = {i for row in tank_table(tank_config(TankSetTarget())) for i in row["marker_ids"]}
    assert min(ids) == 17 and max(ids) == 176  # calibration board uses DICT_6X6_250 IDs 0-16
    # Front/Rear use DICT_5X5_100, whose codes the larger 5x5 dictionaries repeat; 6x6 shares none.
    assert dictionary_for("DICT_6X6_250").markerSize != dictionary_for("DICT_5X5_100").markerSize


def test_marker_id_maps_back_to_its_tank(tank_set):
    directory, _, _ = tank_set
    manifest = load_manifest(directory / "board_manifest.json")
    assert tank_for_marker_id(manifest, 17) == 1
    assert tank_for_marker_id(manifest, 70) == 7
    assert tank_for_marker_id(manifest, 176) == 20
    assert tank_for_marker_id(manifest, 3) is None


@pytest.mark.parametrize(("target", "message"), [
    (TankSetTarget(squares=(3, 3)), "4 internal corners; a trusted coordinate needs at least 6"),
    (TankSetTarget(count=30), "first ID must be"),
    (TankSetTarget(count=0), "from 1 to 100"),
    (TankSetTarget(square_length_mm=0), "square_length_mm"),
])
def test_untrustworthy_or_impossible_sets_are_refused(target, message):
    with pytest.raises(ConfigError, match=message):
        tank_config(target)


def test_smallest_trusted_board_is_allowed():
    config = tank_config(TankSetTarget(count=2, squares=(4, 3)))
    assert config.geometry.corner_count == 6


def test_numbered_boards_get_a_short_combined_sheet_name():
    names = [f"tank{n:02d}" for n in range(1, 21)]
    assert combined_pdf_name(names, "A4", {}) == "tank01-20_A4.pdf"
    assert combined_pdf_name(["front", "rear"], "A4", {}) == "front_rear_A4.pdf"
    assert combined_pdf_name(["tank01", "tank03", "tank04"], "A4", {}) == "tank01_tank03_tank04_A4.pdf"


def test_cli_prints_the_tank_table(tmp_path, capsys):
    assert main(["tanks", "--count", "3", "--first-tank", "5", "--dpi", "150", "--output-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "5  tank05   17-24        8 markers, 9 corners" in out
    assert "7  tank07   33-40" in out and (tmp_path / "tank05-07_A4.pdf").is_file()
    assert main(["verify", "--manifest", str(tmp_path / "board_manifest.json")]) == 0


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler optional PDF render integration test")
def test_every_tank_reads_back_from_the_printed_sheet(tank_set, tmp_path):
    directory, config, _ = tank_set
    subprocess.run(["pdftoppm", "-r", "150", "-png", str(directory / "tank01-20_A4.pdf"), str(tmp_path / "page")],
                   check=True, capture_output=True)
    manifest = load_manifest(directory / "board_manifest.json")
    detector = cv2.aruco.ArucoDetector(dictionary_for("DICT_6X6_250"))
    front_rear = cv2.aruco.ArucoDetector(dictionary_for("DICT_5X5_100"))
    seen: dict[int, set[int]] = {}
    for page in sorted(tmp_path.glob("page-*.png")):
        image = cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)
        assert front_rear.detectMarkers(image)[1] is None
        for marker_id in detector.detectMarkers(image)[1].ravel().tolist():
            seen.setdefault(tank_for_marker_id(manifest, marker_id), set()).add(marker_id)
    assert {tank: sorted(ids) for tank, ids in seen.items()} == {
        row["tank"]: row["marker_ids"] for row in tank_table(config)}


@pytest.mark.skipif(sys.platform == "win32", reason="Bash launcher")
def test_shell_launcher(tmp_path):
    result = subprocess.run([str(ROOT / "generate_tanks.sh"), "--count", "2", "--dpi", "150", "--output-dir",
                             str(tmp_path / "t")], cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "tank02" in result.stdout
