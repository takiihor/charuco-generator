import itertools
import shutil
import subprocess
from dataclasses import replace

import cv2
import numpy as np
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter

from charuco_generator.board import reconstruct_board, sha256
from charuco_generator.config import ConfigError, Geometry
from charuco_generator.generator import generate
from charuco_generator.render import plan_pages, raster_metrics
from charuco_generator.validation import (
    ValidationError,
    detector_parameters,
    load_manifest,
    unexpected_files,
    validate_artifacts,
    validate_image,
    validate_pdf,
)


def test_generated_files_and_checksum_scope(artifacts):
    directory, manifest, report = artifacts
    assert {p.name for p in directory.iterdir()} == {
        "front_charuco.png", "rear_charuco.png", "front_charuco.pdf", "rear_charuco.pdf",
        "front_rear_A4.pdf", "board_manifest.json", "validation_report.json"}
    assert report["passed"]
    assert report["marker_id_sets_disjoint"]
    assert report["physical_accuracy_verified"] is False
    assert len(manifest["sha256"]) == 5
    assert all(report["checksums_match"].values())
    assert report["manifest_sha256"] == sha256(directory / "board_manifest.json")


@pytest.mark.parametrize("index", [0, 1])
def test_png_geometry_and_synthetic_detection(artifacts, index):
    directory, manifest, report = artifacts
    record = manifest["boards"][index]
    with Image.open(directory / record["files"]["png"]) as png:
        assert png.size == (2832, 2360)
        assert png.mode == "L"
        assert set(np.unique(png)) == {0, 255}
    metrics = record["raster"]
    assert metrics["pattern_pixels"] == [2360, 1888]
    assert metrics["effective_pattern_dpi_at_exact_size"] == pytest.approx(599.44)
    assert abs(metrics["square_rounding_px"]) <= 0.5
    result = report["boards"][index]
    assert result["passed"]
    assert result["detected_marker_ids"] == list(range(index * 10 + 1, index * 10 + 11))
    assert result["detected_charuco_ids"] == list(range(12))
    assert all(v for k, v in result["synthetic_checks"].items() if k.endswith(("match", "clear")))


@pytest.mark.parametrize("index", [0, 1])
def test_front_rear_cannot_be_confused(artifacts, index):
    directory, manifest, _ = artifacts
    selected = manifest["boards"][index]
    wrong_input = manifest["boards"][1 - index]["files"]["png"]
    result = validate_image(directory / wrong_input, selected, reconstruct_board(manifest, selected["name"]))
    assert not result["passed"]
    assert result["matched_marker_ids"] == []
    assert result["detected_charuco_corner_count"] == 0
    assert result["foreign_marker_ids"]


def test_photograph_path_with_perspective_and_jpeg(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    record = manifest["boards"][0]
    image = cv2.imread(str(directory / record["files"]["png"]), cv2.IMREAD_GRAYSCALE)
    image = cv2.resize(image, None, fx=0.45, fy=0.45, interpolation=cv2.INTER_AREA)
    h, w = image.shape
    source = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    target = np.float32([[35, 15], [w - 45, 50], [w - 12, h - 30], [10, h - 12]])
    photo = cv2.warpPerspective(image, cv2.getPerspectiveTransform(source, target), (w, h), borderValue=255)
    path = tmp_path / "simulated_photo.jpg"
    assert cv2.imwrite(str(path), photo, [cv2.IMWRITE_JPEG_QUALITY, 80])
    result = validate_image(path, record, reconstruct_board(manifest, "front"))
    assert result["passed"]
    assert result["detected_marker_count"] == 10
    assert result["detected_charuco_corner_count"] == 12
    assert result["physical_accuracy_verified"] is False


def test_partial_photo_and_strict_mode(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    record = manifest["boards"][0]
    image = cv2.imread(str(directory / record["files"]["png"]), cv2.IMREAD_GRAYSCALE)
    image[:, 2100:] = 255  # Occlude the last chessboard column.
    path = tmp_path / "partial.png"
    cv2.imwrite(str(path), image)
    board = reconstruct_board(manifest, "front")
    partial = validate_image(path, record, board)
    assert partial["passed"]
    assert partial["missing_marker_ids"]
    assert not partial["full_expected_visibility"]
    assert not validate_image(path, record, board, require_complete=True)["passed"]


def test_blank_missing_and_bad_geometry_fail(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    record = manifest["boards"][0]
    board = reconstruct_board(manifest, "front")
    blank = tmp_path / "blank.png"
    Image.new("L", (500, 500), 255).save(blank)
    assert not validate_image(blank, record, board)["passed"]
    assert not validate_image(tmp_path / "absent.png", record, board)["passed"]
    original = Image.open(directory / record["files"]["png"])
    wrong = tmp_path / "resized.png"
    original.resize((1416, 1180)).save(wrong)
    result = validate_image(wrong, record, board, synthetic=True)
    assert not result["passed"]
    assert not result["synthetic_checks"]["dimensions_match"]


def test_pdf_pattern_and_page_dimensions(artifacts):
    directory, _, report = artifacts
    assert all(pdf["passed"] for pdf in report["pdfs"])
    combined = next(pdf for pdf in report["pdfs"] if pdf["input"] == "front_rear_A4.pdf")
    assert combined["page_count"] == 1
    assert combined["page_dimensions_mm"] == [210, 297]
    assert len(combined["patterns"]) == 2
    for pattern in combined["patterns"]:
        assert pattern["pattern_bbox_mm_xywh"][2:] == pytest.approx([100, 80], abs=0.00005)
    # Both axes carry a bar per panel: measuring only one of them cannot reveal anisotropic scaling.
    assert sorted(r["axis"] for r in combined["reference_lines"]) == ["horizontal", "horizontal", "vertical", "vertical"]
    assert all(round(r["length_mm"], 3) == 50.0 for r in combined["reference_lines"])
    # Check actual encoded vector form extent via pypdf as well as generator report.
    page = PdfReader(directory / "front_charuco.pdf").pages[0]
    form = page["/Resources"]["/XObject"]["/FormXob.Charuco_front"].get_object()
    assert list(map(float, form["/BBox"])) == pytest.approx([0, 0, 100 * 72 / 25.4, 80 * 72 / 25.4], abs=0.001)


def test_scaled_pdf_is_rejected(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    reader = PdfReader(directory / "front_charuco.pdf")
    reader.pages[0].scale_by(0.95)
    writer = PdfWriter()
    writer.add_page(reader.pages[0])
    damaged = tmp_path / "scaled.pdf"
    writer.write(damaged)
    result = validate_pdf(damaged, manifest, manifest["pdf_layouts"]["front_charuco.pdf"])
    assert not result["passed"]
    assert any("physical page size" in error or "transform changed" in error for error in result["failures"])


def test_print_layout_has_clear_annotation_bands(config):
    placements = plan_pages(config, config.boards)
    for placement in placements:
        px, py, _, ph = placement.panel_bbox_mm
        assert placement.x_mm >= px + config.printing.white_margin_mm
        assert placement.y_mm >= py + 10 + config.printing.white_margin_mm
        assert placement.y_mm + placement.height_mm + config.printing.white_margin_mm <= py + ph - 14 + 1e-9
    assert placements[0].y_mm > placements[1].y_mm + placements[1].height_mm + 2 * config.printing.white_margin_mm


def test_oversized_layout_rejected_before_writing(config, tmp_path):
    bad = replace(config, geometry=replace(config.geometry, square_length_mm=50))
    target = tmp_path / "oversized"
    with pytest.raises(ConfigError, match="exceed printable page"):
        generate(bad, target)
    assert not target.exists()


def test_more_pages_instead_of_scaling(config, tmp_path):
    config = replace(config, geometry=replace(config.geometry, square_length_mm=22))
    report = generate(config, tmp_path)
    combined = next(pdf for pdf in report["pdfs"] if pdf["input"] == "front_rear_A4.pdf")
    assert combined["page_count"] == 2
    assert all(p["pattern_bbox_mm_xywh"][2:] == pytest.approx([110, 88], abs=0.0001) for p in combined["patterns"])


def test_deterministic_outputs(config, artifacts, tmp_path):
    original, _, _ = artifacts
    generate(config, tmp_path)
    for path in original.iterdir():
        assert path.read_bytes() == (tmp_path / path.name).read_bytes(), path.name


def test_changed_png_checksum_and_margin_rejected(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    for path in directory.iterdir():
        shutil.copy2(path, tmp_path / path.name)
    damaged = tmp_path / "front_charuco.png"
    with Image.open(damaged) as image:
        image.putpixel((0, 0), 0)
        image.save(damaged)
    result = validate_artifacts(tmp_path, manifest)
    assert not result["passed"]
    assert not result["checksums_match"]["front_charuco.png"]


def test_failure_is_not_published(config, tmp_path, monkeypatch):
    monkeypatch.setattr("charuco_generator.generator.validate_artifacts", lambda *a: {"passed": False})
    target = tmp_path / "failed"
    with pytest.raises(ValidationError):
        generate(config, target)
    assert not target.exists()


def test_missing_pdf_returns_validation_failure(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    for path in directory.iterdir():
        if path.name != "rear_charuco.pdf":
            shutil.copy2(path, tmp_path / path.name)
    result = validate_artifacts(tmp_path, manifest)
    assert not result["passed"]
    assert not result["checksums_match"]["rear_charuco.pdf"]


def test_raster_resource_guards(config):
    with pytest.raises(ConfigError, match="module"):
        raster_metrics(replace(config.geometry, square_length_mm=0.5, marker_length_mm=0.2), config.printing)
    with pytest.raises(ConfigError, match="megapixel"):
        raster_metrics(replace(config.geometry, square_length_mm=300), replace(config.printing, dpi=2400))


@pytest.mark.parametrize("legacy,squares", [(True, (5, 4)), (False, (4, 5)), (True, (3, 3)), (False, (2, 2))])
def test_other_layouts_and_permuted_ids(config, tmp_path, legacy, squares):
    x, y = squares
    count = x * y // 2
    spec = replace(config.boards[0], marker_ids=tuple(reversed(range(20, 20 + count))))
    config = replace(config, geometry=replace(config.geometry, squares_x=x, squares_y=y, legacy_pattern=legacy), boards=(spec,))
    report = generate(config, tmp_path)
    assert report["passed"]
    assert report["boards"][0]["detected_charuco_corner_count"] == (x - 1) * (y - 1)


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="Poppler optional PDF render integration test")
def test_rendered_vector_pdf_decodes_both_boards(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    subprocess.run(["pdftoppm", "-r", "180", "-singlefile", "-png", str(directory / "front_rear_A4.pdf"), str(tmp_path / "render")],
                   check=True, capture_output=True)
    for record in manifest["boards"]:
        result = validate_image(tmp_path / "render.png", record, reconstruct_board(manifest, record["name"]))
        assert result["passed"]
        assert result["matched_marker_ids"] == record["marker_ids"]
        assert result["detected_marker_count"] == 20
        assert result["detected_charuco_corner_count"] == 12


def test_photograph_reports_marker_sampling_resolution(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    record = manifest["boards"][0]
    board = reconstruct_board(manifest, "front")
    image = cv2.imread(str(directory / record["files"]["png"]), cv2.IMREAD_GRAYSCALE)
    photo = cv2.resize(image, None, fx=0.45, fy=0.45, interpolation=cv2.INTER_AREA)
    path = tmp_path / "photo.png"
    cv2.imwrite(str(path), photo)
    result = validate_image(path, record, board)
    assert result["passed"], result["failures"]
    # Roughly 330 px per 7-module marker at full size, so a 0.45 scale keeps ~21 px per module.
    assert 10 < result["detection_quality"]["min_pixels_per_module"] < 30
    assert result["detection_quality"]["modules_per_marker"] == 7
    assert result["warnings"] == []


def test_under_resolved_photograph_gets_a_quality_warning(artifacts, tmp_path, monkeypatch):
    directory, manifest, _ = artifacts
    record = manifest["boards"][0]
    board = reconstruct_board(manifest, "front")
    image = cv2.imread(str(directory / record["files"]["png"]), cv2.IMREAD_GRAYSCALE)
    path = tmp_path / "photo.png"
    cv2.imwrite(str(path), cv2.resize(image, None, fx=0.45, fy=0.45, interpolation=cv2.INTER_AREA))
    monkeypatch.setattr("charuco_generator.validation.MIN_PIXELS_PER_MODULE", 60.0)
    result = validate_image(path, record, board)
    assert result["passed"]  # Under-resolution is reported as a warning, not a hard failure.
    assert any("pixels per module" in warning for warning in result["warnings"])
    assert any("Move closer" in warning for warning in result["warnings"])


def test_detector_overrides_apply_and_are_range_checked(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    record = manifest["boards"][0]
    board = reconstruct_board(manifest, "front")
    image = cv2.imread(str(directory / record["files"]["png"]), cv2.IMREAD_GRAYSCALE)
    path = tmp_path / "photo.png"
    cv2.imwrite(str(path), cv2.resize(image, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA))
    geometry = Geometry(**record["geometry"])
    assert detector_parameters(geometry).cornerRefinementMethod == cv2.aruco.CORNER_REFINE_NONE
    assert detector_parameters(geometry, {"corner_refinement": "subpix"}).cornerRefinementMethod \
        == cv2.aruco.CORNER_REFINE_SUBPIX
    tuned = validate_image(path, record, board, detector_overrides={"adaptive_thresh_window": 15})
    assert tuned["detected_marker_count"] == 10
    for overrides, expected in (({"min_perimeter_rate": 5.0}, "min_perimeter_rate"),
                                ({"corner_refinement": "magic"}, "corner refinement"),
                                ({"not_a_parameter": 1}, "Unknown detector parameter")):
        with pytest.raises(ConfigError, match=expected):
            detector_parameters(geometry, overrides)


def test_stale_artifacts_are_named_and_fail_verification(artifacts, tmp_path):
    directory, manifest, _ = artifacts
    for path in directory.iterdir():
        shutil.copy2(path, tmp_path / path.name)
    (tmp_path / "old_grid_charuco.png").write_bytes(b"leftover from an earlier run")
    (tmp_path / "notes.txt").write_text("not an artifact")
    result = validate_artifacts(tmp_path, manifest)
    assert result["unexpected_files"] == ["old_grid_charuco.png"]
    assert not result["passed"]
    assert "delete them or regenerate with --clean" in result["unexpected_files_note"]
    assert unexpected_files(tmp_path / "absent", set()) == []


def test_generate_refuses_a_dirty_folder_until_cleaned(tmp_path, config):
    target = tmp_path / "set"
    generate(config, target)
    leftover = target / "old_grid_charuco.pdf"
    leftover.write_bytes(b"%PDF-1.4 fake")
    with pytest.raises(ConfigError, match="does not produce") as excinfo:
        generate(config, target)
    assert "--clean to delete them" in str(excinfo.value)
    assert leftover.is_file()
    report = generate(config, target, clean=True)
    assert report["passed"] and report["unexpected_files"] == []
    assert not leftover.exists()
    (target / "notes.txt").write_text("keep me")
    assert generate(config, target, clean=True)["passed"]
    assert (target / "notes.txt").read_text() == "keep me"


def test_vector_exports_are_checksummed_not_print_files(config, tmp_path):
    target = tmp_path / "exports"
    generate(config, target, svg=True, dxf=True, dictionary_file=True)
    manifest = load_manifest(target / "board_manifest.json")
    assert manifest["exports"] == ["vector/DICT_5X5_100.yml", "vector/front.dxf", "vector/front.svg",
                                   "vector/rear.dxf", "vector/rear.svg"]
    assert set(manifest["sha256"]) > set(manifest["exports"])
    assert unexpected_files(target, set(manifest["sha256"])) == []
    assert validate_artifacts(target, manifest)["passed"]
    svg = (target / "vector/front.svg").read_text()
    dxf = (target / "vector/front.dxf").read_text()
    assert 'width="100.000000mm"' in svg and 'viewBox="0 0 100.000000 80.000000"' in svg
    assert "AC1009" in dxf and dxf.count("POLYLINE") == svg.count("<rect") - 1
    assert "nmarkers" in (target / "vector/DICT_5X5_100.yml").read_text()
    (target / "vector/front.svg").write_text("<svg/>")
    tampered = validate_artifacts(target, manifest)
    assert not tampered["passed"]
    assert not tampered["checksums_match"]["vector/front.svg"]


def test_manifest_without_exports_key_still_verifies(config, tmp_path):
    target = tmp_path / "legacy"
    generate(config, target)
    manifest = load_manifest(target / "board_manifest.json")
    del manifest["exports"]
    assert validate_artifacts(target, manifest)["passed"]
    manifest["exports"] = ["../outside.svg"]
    with pytest.raises(ConfigError, match="vector/ subdirectory"):
        validate_artifacts(target, manifest)


def test_tiled_sheets_keep_measurable_bars_and_reassemble(config, tmp_path):
    wide = replace(config, geometry=replace(config.geometry, squares_x=10, squares_y=8,
                                             square_length_mm=30.0, marker_length_mm=21.0),
                   printing=replace(config.printing, tile_overlap_mm=12.0, dpi=150),
                   boards=(replace(config.boards[0], marker_ids=tuple(range(40))),))
    target = tmp_path / "tiled"
    report = generate(wide, target)
    layout = load_manifest(target / "board_manifest.json")["pdf_layouts"]["front_charuco.pdf"]
    assert len(layout) > 1 and len({placement["page"] for placement in layout}) == len(layout)
    strips = sorted({(p["subrect_mm"][0], p["subrect_mm"][2]) for p in layout})
    assert strips[0][0] == 0 and strips[-1][1] == pytest.approx(300)
    assert all(b[0] <= a[1] - 12 + 1e-9 for a, b in itertools.pairwise(strips))
    combined = next(pdf for pdf in report["pdfs"] if pdf["input"] == "front_A4.pdf")
    assert {line["axis"] for line in combined["reference_lines"]} == {"horizontal", "vertical"}
    assert len({line["page"] for line in combined["reference_lines"]}) == combined["page_count"]
    assert all("tile" in pattern and "subrect_mm" in pattern for pattern in combined["patterns"])


def test_oversized_board_without_tiling_says_how_to_proceed(config, tmp_path):
    wide = replace(config, geometry=replace(config.geometry, squares_x=10, squares_y=8,
                                             square_length_mm=30.0, marker_length_mm=21.0),
                   boards=(replace(config.boards[0], marker_ids=tuple(range(40))),))
    with pytest.raises(ConfigError, match=r"--tile-overlap-mm N") as excinfo:
        generate(wide, tmp_path / "tiled")
    assert "Automatic scaling is forbidden" in str(excinfo.value)
    assert not (tmp_path / "tiled").exists()


def test_tall_narrow_board_tiles_stay_inside_the_pattern(config, tmp_path):
    tall = replace(config, geometry=replace(config.geometry, squares_x=4, squares_y=12,
                                            square_length_mm=30.0, marker_length_mm=21.0),
                   printing=replace(config.printing, tile_overlap_mm=10.0, dpi=150),
                   boards=(replace(config.boards[0], marker_ids=tuple(range(24))),))
    layout = plan_pages(tall, tall.boards)
    assert len(layout) > 1
    for placement in layout:
        x0, y0, x1, y1 = placement.subrect_mm
        assert 0 <= x0 < x1 <= 120 and 0 <= y0 < y1 <= 360
        # Width fits the page, so every tile carries the full width and the edge marks sit on the pattern.
        assert (x0, x1) == (0, 120) and placement.width_mm == 120
    assert generate(tall, tmp_path / "tall")["passed"]


def test_dictionary_quality_mismatch_from_older_generator_only_warns(artifacts):
    directory, manifest, _ = artifacts
    edited = dict(manifest, dictionary_quality={**manifest["dictionary_quality"], "min_hamming_distance": 6})
    current = validate_artifacts(directory, edited)
    assert not current["passed"] and current["quality_failures"]
    older = validate_artifacts(directory, dict(edited, generator_version="1.0.0"))
    assert older["passed"] and not older["quality_failures"]
    assert any("earlier formula" in warning for warning in older["warnings"])
