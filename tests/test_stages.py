import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from charuco_generator.aruco import ArucoConfig
from charuco_generator.cli import main
from charuco_generator.config import ConfigError, dictionary_for
from charuco_generator.stages import (
    check_collisions,
    code_family,
    configs,
    existing_conflicts,
    generate_plan,
    lookup,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    root = tmp_path_factory.mktemp("two_stage")
    return root, generate_plan(root, dpi=200)


def test_plan_ids_follow_the_agreed_scheme(tmp_path):
    plan = configs(16, tmp_path)
    assert plan["stage1_aruco_realsense"].marker_ids == tuple(range(1, 17))
    assert plan["stage1_aruco_realsense"].marker_length_mm == 150.0
    for name, square in (("stage2_charuco_basler_32mm", 8.0), ("stage2_charuco_realsense_80mm", 20.0)):
        config = plan[name]
        assert config.geometry.dictionary == "DICT_6X6_250" and config.geometry.square_length_mm == square
        assert [b.marker_ids for b in config.boards] == [tuple(range(17 + 8 * i, 25 + 8 * i)) for i in range(16)]
    assert plan["calibration_stage1"].boards[0].marker_ids == tuple(range(145, 167))
    assert plan["calibration_stage1"].geometry.pattern_mm == (360.0, 200.0)
    assert plan["calibration_stage2"].boards[0].marker_ids == tuple(range(167, 184))
    assert plan["calibration_stage2"].geometry.pattern_mm == (75.0, 105.0)
    rows = lookup(plan)
    assert rows[6] == {"tank": 7, "stage1_marker_id": 7, "stage2_board": "tank07",
                       "stage2_marker_ids": list(range(65, 73))}


@pytest.mark.parametrize("size", [4, 5, 6, 7])
def test_same_size_dictionaries_share_codes_so_families_are_checked(size):
    small = dictionary_for(f"DICT_{size}X{size}_50").bytesList
    large = dictionary_for(f"DICT_{size}X{size}_1000").bytesList
    assert np.array_equal(large[:50], small)
    assert code_family(f"DICT_{size}X{size}_50") == code_family(f"DICT_{size}X{size}_1000")


def test_collisions_are_refused_before_generation(tmp_path):
    plan = configs(16, tmp_path)
    check_collisions(plan)  # the agreed plan is clean; the two stage-2 sizes may share
    plan["stage1_aruco_realsense"] = ArucoConfig("DICT_6X6_250", 150.0, marker_ids=(20, 21))
    with pytest.raises(ConfigError, match="share 6x6 marker codes"):
        check_collisions(plan)
    with pytest.raises(ConfigError, match="1 to 24 tanks"):
        configs(25, tmp_path)


def test_front_rear_overlap_is_reported(tmp_path):
    warnings = existing_conflicts(configs(16, tmp_path), ROOT / "config" / "boards.json")
    assert len(warnings) == 1 and "stage1_aruco_realsense" in warnings[0] and "front, rear" in warnings[0]


def test_every_set_is_generated_verified_and_described(generated):
    root, result = generated
    plan = json.loads((root / "marker_plan.json").read_text())
    assert set(plan["sets"]) == set(result["reports"]) and all(r["passed"] for r in result["reports"].values())
    assert len(plan["tank_lookup"]) == 16 and plan["tank_lookup"][0]["stage2_marker_ids"] == list(range(17, 25))
    for entry in plan["sets"].values():
        assert (root / entry["manifest"]).is_file()
    for name in ("stage2_charuco_basler_32mm", "stage2_charuco_realsense_80mm"):
        assert all((b["detected_marker_count"], b["detected_charuco_corner_count"]) == (8, 9)
                   for b in result["reports"][name]["boards"])


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler optional PDF render integration test")
def test_printed_sets_decode_only_in_their_own_dictionary(generated, tmp_path):
    root, _ = generated
    detectors = {name: cv2.aruco.ArucoDetector(dictionary_for(name)) for name in ("DICT_5X5_100", "DICT_6X6_250")}
    sheets = {"stage1_aruco_realsense/aruco_1-16_A4.pdf": ("DICT_5X5_100", range(1, 17)),
              "stage2_charuco_basler_32mm/tank01-16_A4.pdf": ("DICT_6X6_250", range(17, 145))}
    for sheet, (own, expected) in sheets.items():
        prefix = tmp_path / Path(sheet).parent.name
        subprocess.run(["pdftoppm", "-r", "300", "-png", str(root / sheet), str(prefix)], check=True,
                       capture_output=True)
        seen = {name: [] for name in detectors}
        for page in sorted(tmp_path.glob(f"{prefix.name}-*.png")):
            image = cv2.imread(str(page), cv2.IMREAD_GRAYSCALE)
            for name, detector in detectors.items():
                ids = detector.detectMarkers(image)[1]
                seen[name] += [] if ids is None else ids.ravel().tolist()
        assert sorted(seen.pop(own)) == list(expected), sheet
        assert not next(iter(seen.values())), sheet


def test_cli_prints_lookup_and_warning(tmp_path, capsys):
    assert main(["two-stage", "--tanks", "2", "--dpi", "200", "--output-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "     2           2  25-32" in captured.out
    assert "shares 5x5 codes 1-2" in captured.err
    assert main(["two-stage", "--tanks", "2", "--dpi", "200", "--output-dir", str(tmp_path),
                 "--boards-config", ""]) == 0  # regeneration into the same folders is allowed


def test_calibration_boards_can_be_named_apart(tmp_path):
    from charuco_generator.calibration import CalibrationTarget, calibration_config
    named = calibration_config(replace(CalibrationTarget(), name="calibration_stage2"), str(tmp_path))
    assert named.boards[0].name == "calibration_stage2"
