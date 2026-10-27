import json
import shutil
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from charuco_generator.aruco import (
    ArucoConfig,
    generate_aruco,
    validate_aruco_artifacts,
)
from charuco_generator.cli import main
from charuco_generator.config import ConfigError, Printing, dictionary_for
from charuco_generator.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def aruco_artifacts(tmp_path_factory):
    directory = tmp_path_factory.mktemp("aruco")
    report = generate_aruco(ArucoConfig(), directory)
    manifest = json.loads((directory / "aruco_manifest.json").read_text())
    return directory, manifest, report


def test_all_twenty_saved_markers_decode_with_clear_margins(aruco_artifacts):
    directory, manifest, report = aruco_artifacts
    assert report["passed"]
    assert manifest["marker_ids"] == list(range(1, 21))
    assert len(list(directory.glob("*.png"))) == 20
    assert len(list(directory.glob("*.pdf"))) == 21
    detector = cv2.aruco.ArucoDetector(dictionary_for("DICT_5X5_100"))
    metrics = manifest["raster"]
    for marker_id in range(1, 21):
        with Image.open(directory / f"aruco_{marker_id:02d}.png") as image:
            pixels = np.asarray(image)
            assert image.mode == "L"
            assert image.size == tuple(metrics["image_pixels"])
            assert set(np.unique(pixels)) == {0, 255}
            margin = metrics["margin_pixels"]
            assert np.all(pixels[:margin] == 255)
            assert np.all(pixels[-margin:] == 255)
            assert np.all(pixels[:, :margin] == 255)
            assert np.all(pixels[:, -margin:] == 255)
        _, ids, _ = detector.detectMarkers(pixels)
        assert ids.ravel().tolist() == [marker_id]
    assert [item["detected_marker_ids"] for item in report["markers"]] == [[i] for i in range(1, 21)]
    assert all(pdf["passed"] for pdf in report["pdfs"])


def test_cli_custom_settings_and_verification(tmp_path, capsys):
    assert main(["aruco", "--marker-size-mm", "40", "--dictionary", "DICT_4X4_50",
                 "--border-bits", "2", "--dpi", "300", "--page-size", "A3",
                 "--white-margin-mm", "8", "--page-margin-mm", "12", "--output-dir", str(tmp_path)]) == 0, capsys.readouterr()
    manifest_path = tmp_path / "aruco_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["configuration"] == {"dictionary": "DICT_4X4_50", "marker_length_mm": 40.0,
                                          "border_bits": 2, "printing": {"dpi": 300, "page_size": "A3",
                                                                            "white_margin_mm": 8.0,
                                                                            "page_margin_mm": 12.0,
                                                                            "tile_overlap_mm": 0.0},
                                          "marker_ids": list(range(1, 21))}
    assert manifest["marker_ids"] == list(range(1, 21))
    assert main(["verify", "--manifest", str(manifest_path), "--report", str(tmp_path / "verification.json")]) == 0
    assert json.loads((tmp_path / "verification.json").read_text())["passed"]


@pytest.mark.parametrize("options", [
    ["--marker-size-mm", "0"], ["--marker-size-mm", "nan"], ["--marker-size-mm", "inf"],
    ["--marker-size-mm", "0.1"], ["--marker-size-mm", "250"],
    ["--marker-size-mm", "150", "--dpi", "2400", "--page-size", "A3"],
    ["--dictionary", "DICT_MISSING"], ["--dpi", "0"], ["--white-margin-mm", "2"],
    ["--border-bits", "0"], ["--page-size", "bad"], ["--page-margin-mm", "-1"],
])
def test_invalid_requests_publish_nothing(tmp_path, options):
    target = tmp_path / "invalid"
    assert main(["aruco", *options, "--output-dir", str(target)]) == 2
    assert not target.exists()


def test_dictionary_must_contain_every_requested_id():
    # DICT_4X4_50 holds IDs 0..49, so an ID beyond that cannot be printed with it.
    with pytest.raises(ConfigError, match=r"DICT_4X4_50 contains IDs 0\.\.49, so \[60\] cannot be printed"):
        ArucoConfig(dictionary="DICT_4X4_50", marker_ids=(0, 60)).validate()


def test_custom_id_subset_generates_and_verifies(tmp_path):
    assert main(["aruco", "--ids", "3,7,40-42", "--output-dir", str(tmp_path)]) == 0
    manifest = json.loads((tmp_path / "aruco_manifest.json").read_text())
    assert manifest["marker_ids"] == [3, 7, 40, 41, 42]
    assert {name for name in manifest["sha256"] if name.endswith(".png")} == {
        "aruco_03.png", "aruco_07.png", "aruco_40.png", "aruco_41.png", "aruco_42.png"}
    assert main(["verify", "--manifest", str(tmp_path / "aruco_manifest.json")]) == 0


@pytest.mark.parametrize("ids", ["20-18", "1,,2", "a", "-5", "1.5"])
def test_invalid_id_lists_are_rejected(tmp_path, ids):
    target = tmp_path / "run"
    assert main(["aruco", "--ids", ids, "--output-dir", str(target)]) == 2
    assert not target.exists()


def test_failed_validation_preserves_previous_files(tmp_path, monkeypatch):
    existing = tmp_path / "aruco_01.png"
    existing.write_bytes(b"previous output")
    monkeypatch.setattr("charuco_generator.aruco.validate_aruco_artifacts", lambda *args: {"passed": False})
    with pytest.raises(ValidationError):
        generate_aruco(ArucoConfig(), tmp_path)
    assert existing.read_bytes() == b"previous output"
    assert list(tmp_path.iterdir()) == [existing]


def test_moved_outputs_verify_and_damage_is_detected(aruco_artifacts, tmp_path):
    directory, manifest, _ = aruco_artifacts
    shutil.copytree(directory, tmp_path, dirs_exist_ok=True)
    assert validate_aruco_artifacts(tmp_path, manifest)["passed"]
    shutil.copyfile(tmp_path / "aruco_02.png", tmp_path / "aruco_01.png")
    (tmp_path / "aruco_20.pdf").unlink()
    reader = PdfReader(tmp_path / "aruco_03.pdf")
    reader.pages[0].scale_by(0.95)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    writer.write(tmp_path / "aruco_03.pdf")
    result = validate_aruco_artifacts(tmp_path, manifest)
    assert not result["passed"]
    assert not result["markers"][0]["passed"]
    assert result["markers"][0]["detected_marker_ids"] == [2]
    for filename in ("aruco_01.png", "aruco_03.pdf", "aruco_20.pdf"):
        assert not result["checksums_match"][filename]
    assert not next(pdf for pdf in result["pdfs"] if pdf["input"] == "aruco_03.pdf")["passed"]
    assert not next(pdf for pdf in result["pdfs"] if pdf["input"] == "aruco_20.pdf")["passed"]
    assert main(["verify", "--manifest", str(tmp_path / "aruco_manifest.json")]) == 1


@pytest.mark.parametrize("field", ["marker_ids", "raster", "pdf_layouts", "sha256", "configuration"])
def test_inconsistent_manifest_is_rejected(aruco_artifacts, field):
    directory, manifest, _ = aruco_artifacts
    damaged = deepcopy(manifest)
    if field == "configuration":
        damaged[field]["marker_length_mm"] = 31
    else:
        damaged[field] = [] if field == "marker_ids" else {}
    with pytest.raises(ConfigError):
        validate_aruco_artifacts(directory, damaged)


def test_deterministic_generation(aruco_artifacts, tmp_path):
    directory, _, _ = aruco_artifacts
    generate_aruco(ArucoConfig(), tmp_path)
    for original in directory.iterdir():
        assert original.read_bytes() == (tmp_path / original.name).read_bytes(), original.name


def test_larger_markers_add_pages_without_scaling(tmp_path):
    report = generate_aruco(replace(ArucoConfig(), marker_length_mm=90, printing=Printing(dpi=150)), tmp_path)
    combined = next(pdf for pdf in report["pdfs"] if pdf["input"] == "aruco_1-20_A4.pdf")
    assert combined["page_count"] == 20
    assert all(pattern["pattern_bbox_mm_xywh"][2:] == pytest.approx([90, 90], abs=0.001) for pattern in combined["patterns"])


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler optional PDF render integration test")
def test_every_printed_marker_decodes_from_vector_pdf(aruco_artifacts, tmp_path):
    directory, _, _ = aruco_artifacts
    subprocess.run(["pdftoppm", "-r", "150", "-png", str(directory / "aruco_1-20_A4.pdf"), str(tmp_path / "page")],
                   check=True, capture_output=True)
    detector = cv2.aruco.ArucoDetector(dictionary_for("DICT_5X5_100"))
    detected = []
    for page in sorted(tmp_path.glob("page-*.png")):
        _, ids, _ = detector.detectMarkers(cv2.imread(str(page)))
        detected.extend([] if ids is None else ids.ravel().tolist())
    assert sorted(detected) == list(range(1, 21))


@pytest.mark.skipif(sys.platform == "win32",
                    reason="The launcher is a POSIX shell script; Windows runs python generate.py directly")
def test_shell_launcher_from_another_directory(tmp_path):
    output = tmp_path / "markers with spaces"
    result = subprocess.run([str(ROOT / "generate_aruco.sh"), "--marker-size-mm", "25", "--output-dir", str(output)],
                            cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "IDs 1-20" in result.stdout
    manifest = json.loads((output / "aruco_manifest.json").read_text())
    assert manifest["marker_ids"] == list(range(1, 21))
    assert manifest["configuration"]["marker_length_mm"] == 25


def test_marker_exports_are_vector_files_only(tmp_path):
    target = tmp_path / "exported"
    assert main(["aruco", "--ids", "1-3", "--output-dir", str(target), "--export-svg",
                 "--export-dxf", "--export-dictionary-yml"]) == 0
    manifest = json.loads((target / "aruco_manifest.json").read_text())
    assert manifest["exports"] == ["vector/DICT_5X5_100.yml", "vector/aruco_01.dxf", "vector/aruco_01.svg",
                                   "vector/aruco_02.dxf", "vector/aruco_02.svg", "vector/aruco_03.dxf",
                                   "vector/aruco_03.svg"]
    assert all(name in manifest["sha256"] for name in manifest["exports"])
    assert {path.name for path in target.iterdir() if path.suffix in (".svg", ".dxf", ".yml")} == set()
    assert validate_aruco_artifacts(target, manifest)["passed"]
    assert "<rect" in (target / "vector/aruco_01.svg").read_text()


def test_stale_marker_sheets_block_regeneration_until_cleaned(tmp_path, capsys):
    target = tmp_path / "markers"
    assert main(["aruco", "--ids", "1-5", "--output-dir", str(target)]) == 0
    (target / "aruco_99.png").write_bytes(b"from an earlier ID set")
    assert main(["aruco", "--ids", "1-5", "--output-dir", str(target)]) == 2
    assert "aruco_99.png" in capsys.readouterr().err
    assert main(["aruco", "--ids", "1-5", "--output-dir", str(target), "--clean"]) == 0
    assert not (target / "aruco_99.png").exists()
    report = validate_aruco_artifacts(target, json.loads((target / "aruco_manifest.json").read_text()))
    assert report["passed"] and report["unexpected_files"] == []


def test_validate_command_rejects_an_aruco_manifest(tmp_path, capsys):
    target = tmp_path / "markers"
    assert main(["aruco", "--ids", "7", "--output-dir", str(target)]) == 0
    assert main(["validate", "--manifest", str(target / "aruco_manifest.json"), "--board", "front",
                 "--input", str(target / "aruco_07.png")]) == 2
    error = capsys.readouterr().err
    assert "standalone ArUco markers" in error and "verify --manifest" in error


@pytest.mark.parametrize("options", (["--ids", "0-500"], ["--ids", "1-501"]))
def test_marker_count_is_capped_before_rendering(tmp_path, capsys, options):
    target = tmp_path / "many"
    assert main(["aruco", *options, "--output-dir", str(target)]) == 2
    assert "at most 500" in capsys.readouterr().err
    assert not target.exists()
