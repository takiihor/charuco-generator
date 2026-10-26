"""Independent file readback, modern detectors, and PDF content inspection."""
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import ContentStream

from .board import reconstruct_board, sha256
from .config import ConfigError, Geometry, config_from_dict, page_size_mm
from .render import pattern_rectangles_mm, plan_pages, raster_metrics

MM_TO_PT = 72.0 / 25.4
PDF_POINT_TOLERANCE = 0.001  # <= 0.000353 mm; accommodates ReportLab number serialization up to 2 m pages.
PHYSICAL_NOTE = "Image detection and PDF geometry checks do not establish printed dimensions or robot placement accuracy. Measure the physical print independently."


class ValidationError(RuntimeError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__("Generated artifacts failed validation; inspect the validation report")


def load_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _ids(values) -> list[int]:
    return [] if values is None else np.asarray(values).ravel().astype(int).tolist()


def validate_image(path: str | Path, record: dict, board, *, synthetic: bool = False,
                   require_complete: bool = False) -> dict:
    path = Path(path)
    report = {"input": path.name, "board": record["name"], "identifier": record["identifier"],
              "mode": "synthetic" if synthetic else "photograph",
              "physical_accuracy_verified": False, "physical_accuracy_note": PHYSICAL_NOTE}
    failures = []
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        report.update(passed=False, failures=["Cannot load input image"], detected_marker_ids=[],
                      detected_charuco_corner_count=0, detected_charuco_ids=[])
        return report
    geometry = Geometry(**record["geometry"])
    params = cv2.aruco.DetectorParameters()
    params.markerBorderBits = geometry.border_bits
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_NONE
    marker_corners, marker_ids, rejected = cv2.aruco.ArucoDetector(board.getDictionary(), params).detectMarkers(image)
    charuco_params = cv2.aruco.CharucoParameters()
    charuco_params.tryRefineMarkers = False
    charuco_corners, charuco_ids, _, _ = cv2.aruco.CharucoDetector(board, charuco_params, params).detectBoard(image)
    detected = _ids(marker_ids)
    corner_ids = _ids(charuco_ids)
    expected = record["marker_ids"]
    own = sorted(set(detected) & set(expected))
    missing = sorted(set(expected) - set(detected))
    foreign = sorted(set(detected) - set(expected))
    full_visibility = sorted(detected) == sorted(expected) and sorted(corner_ids) == list(range(geometry.corner_count))
    if not own:
        failures.append("No markers belonging to the selected board detected; check board identity, dictionary and image quality")
    if len(detected) != len(set(detected)):
        failures.append("Duplicate marker IDs detected in image; board identity is ambiguous")
    if not (synthetic or require_complete) and len(corner_ids) < 4:
        failures.append("Fewer than four ChArUco corners detected")
    elif not (synthetic or require_complete) and board.checkCharucoCornersCollinear(np.asarray(corner_ids, dtype=np.int32)):
        failures.append("Detected ChArUco corners are collinear")
    if synthetic or require_complete:
        if sorted(detected) != sorted(expected):
            failures.append("Detected marker IDs/count do not exactly match the expected board")
        if sorted(corner_ids) != list(range(geometry.corner_count)):
            failures.append("Expected complete internal ChArUco corner set was not detected")
    report.update(
        image_pixels=[image.shape[1], image.shape[0]], expected_marker_ids=expected,
        detected_marker_ids=sorted(detected), detected_marker_count=len(detected),
        matched_marker_ids=own, missing_marker_ids=missing, foreign_marker_ids=foreign,
        rejected_marker_candidate_count=len(rejected), detected_charuco_ids=sorted(corner_ids),
        detected_charuco_corner_count=len(corner_ids), expected_charuco_corner_count=geometry.corner_count,
        full_expected_visibility=full_visibility,
        charuco_image_points=[{"id": i, "pixel_xy": point.tolist()} for i, point in
                              zip(corner_ids, np.empty((0, 2)) if charuco_corners is None else charuco_corners.reshape(-1, 2))],
    )
    if synthetic:
        metrics = record["raster"]
        margin = metrics["margin_pixels"]
        width, height = metrics["pattern_pixels"]
        dimension_ok = report["image_pixels"] == metrics["image_pixels"]
        # Exact integer cross product, not a loose floating-point aspect test.
        aspect_ok = width * geometry.squares_y == height * geometry.squares_x
        binary_ok = set(np.unique(image).tolist()) <= {0, 255}
        margin_ok = False
        if dimension_ok:
            margin_ok = bool(np.all(image[:margin] == 255) and np.all(image[-margin:] == 255)
                             and np.all(image[:, :margin] == 255) and np.all(image[:, -margin:] == 255))
        if not dimension_ok:
            failures.append("PNG dimensions differ from saved raster layout")
        if not aspect_ok:
            failures.append("Pattern aspect ratio is inconsistent")
        if not binary_ok:
            failures.append("Synthetic image contains values other than black/white")
        if not margin_ok:
            failures.append("Synthetic white margins are missing or contain ink")
        pitch = metrics["pixels_per_square"] / (geometry.square_length_mm / 1000.0)
        corner_errors = []
        for corner_id, point in zip(corner_ids, [] if charuco_corners is None else charuco_corners.reshape(-1, 2)):
            expected_point = board.getChessboardCorners()[corner_id, :2] * pitch + margin - 0.5
            corner_errors.append(float(np.linalg.norm(point - expected_point)))
        centers = {marker_id: np.asarray(points).mean(axis=0)[:2] * pitch + margin - 0.5
                   for marker_id, points in zip(expected, board.getObjPoints(), strict=True)}
        marker_errors = [float(np.linalg.norm(np.asarray(points).reshape(-1, 2).mean(axis=0) - centers[marker_id]))
                         for marker_id, points in zip(detected, marker_corners) if marker_id in centers]
        if corner_errors and max(corner_errors) > 1.5:
            failures.append("ChArUco corner positions differ from the saved board layout")
        if marker_errors and max(marker_errors) > 2.0:
            failures.append("Encoded marker placement differs from the explicit marker sequence")
        with Image.open(path) as png:
            metadata_dpi = png.info.get("dpi")
        dpi_ok = metadata_dpi is not None and all(abs(v - metrics["png_metadata_dpi"]) < 0.03 for v in metadata_dpi)
        if not dpi_ok:
            failures.append("PNG physical-resolution metadata differs from the saved effective DPI")
        report["synthetic_checks"] = {
            "dimensions_match": dimension_ok, "exact_pattern_aspect_ratio": aspect_ok,
            "binary_black_white": binary_ok, "white_margins_clear": margin_ok,
            "png_dpi_metadata_matches": dpi_ok,
            "max_charuco_position_error_px": max(corner_errors, default=None),
            "max_marker_center_error_px": max(marker_errors, default=None),
        }
    report.update(passed=not failures, failures=failures)
    return report


def _matrix(values) -> np.ndarray:
    a, b, c, d, e, f = map(float, values)
    return np.array([[a, c, e], [b, d, f], [0, 0, 1]])


def validate_pdf(path: str | Path, manifest: dict, expected_placements: list[dict]) -> dict:
    """Inspect actual MediaBox, form rectangles, BBox and placement CTMs, not metadata claims."""
    config = config_from_dict(manifest["configuration"])
    rectangles = {spec.name: pattern_rectangles_mm(config.geometry, spec, reconstruct_board(manifest, spec.name))
                  for spec in config.boards}
    return validate_vector_pdf(path, page_size_mm(config.printing.page_size), expected_placements, rectangles)


def validate_vector_pdf(path: str | Path, page_mm: tuple[float, float],
                        expected_placements: list[dict], rectangles: dict) -> dict:
    """Check physical PDF geometry for either boards or standalone marker forms."""
    reader = PdfReader(path)
    failures = []
    observed = []
    references = []
    expected_pages = max(p["page"] for p in expected_placements) + 1
    if len(reader.pages) != expected_pages:
        failures.append("Unexpected PDF page count")
    prefs = reader.trailer["/Root"].get("/ViewerPreferences", {})
    if hasattr(prefs, "get_object"):
        prefs = prefs.get_object()
    if prefs.get("/PrintScaling") != "/None":
        failures.append("PDF does not request PrintScaling=None")
    by_form = {item["pdf_form"]: item for item in expected_placements}
    for page_index, page in enumerate(reader.pages):
        actual_box = list(map(float, page.mediabox))
        target_box = [0, 0, page_mm[0] * MM_TO_PT, page_mm[1] * MM_TO_PT]
        if not np.allclose(actual_box, target_box, rtol=0, atol=PDF_POINT_TOLERANCE):
            failures.append(f"Page {page_index}: physical page size differs")
        if list(page.cropbox) != list(page.mediabox) or page.rotation != 0 or float(page.get("/UserUnit", 1)) != 1:
            failures.append(f"Page {page_index}: unexpected crop, rotation or user unit")
        ctm = np.eye(3)
        stack = []
        current_point = None
        xobjects = page["/Resources"].get("/XObject", {})
        for operands, operation in ContentStream(page.get_contents(), reader).operations:
            if operation == b"q":
                stack.append(ctm.copy())
            elif operation == b"Q":
                ctm = stack.pop()
            elif operation == b"cm":
                ctm = ctm @ _matrix(operands)
            elif operation == b"m":
                current_point = ctm @ np.array([float(operands[0]), float(operands[1]), 1])
            elif operation == b"l" and current_point is not None:
                end = ctm @ np.array([float(operands[0]), float(operands[1]), 1])
                if abs(end[1] - current_point[1]) < 0.0001 and abs(abs(end[0] - current_point[0]) / MM_TO_PT - 50) < 0.0001:
                    references.append({"page": page_index, "length_mm": float(np.linalg.norm(end[:2] - current_point[:2]) / MM_TO_PT)})
                current_point = end
            elif operation == b"Do":
                name = str(operands[0])
                if name not in by_form:
                    failures.append(f"Unexpected PDF drawing object: {name}")
                    continue
                placement = by_form[name]
                form = xobjects[name].get_object()
                transform = ctm @ _matrix(form.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                bbox = np.array(list(map(float, form["/BBox"])))
                x, y, w, h = placement["pattern_bbox_mm_xywh"]
                wanted_transform = _matrix([1, 0, 0, 1, x * MM_TO_PT, y * MM_TO_PT])
                if not np.allclose(transform, wanted_transform, rtol=0, atol=PDF_POINT_TOLERANCE):
                    failures.append(f"{name}: placement transform changed (scaling, translation or distortion)")
                if page_index != placement["page"] or not np.allclose(bbox, [0, 0, w * MM_TO_PT, h * MM_TO_PT], rtol=0, atol=PDF_POINT_TOLERANCE):
                    failures.append(f"{name}: pattern bounding box/page differs from requested physical dimensions")
                # Also check every actual pattern rectangle, including marker modules.
                rects = [[float(v) for v in args] for args, op in ContentStream(form, reader).operations if op == b"re"]
                expected_rects = [[0, 0, w * MM_TO_PT, h * MM_TO_PT]] + [
                    [v * MM_TO_PT for v in rect] for rect in rectangles[placement["board"]]]
                if len(rects) != len(expected_rects) or not np.allclose(rects, expected_rects, rtol=0, atol=PDF_POINT_TOLERANCE):
                    failures.append(f"{name}: vector pattern or marker module geometry differs")
                origin = transform @ np.array([bbox[0], bbox[1], 1])
                opposite = transform @ np.array([bbox[2], bbox[3], 1])
                observed.append({"board": placement["board"], "page": page_index,
                                 "pattern_bbox_mm_xywh": [float(v / MM_TO_PT) for v in [origin[0], origin[1], opposite[0] - origin[0], opposite[1] - origin[1]]],
                                 "vector_rectangle_count": len(rects)})
    if sorted((p["board"], p["page"]) for p in observed) != sorted((p["board"], p["page"]) for p in expected_placements):
        failures.append("Missing or duplicate board forms")
    if len(references) != len(expected_placements):
        failures.append("Missing/incorrect 50 mm reference lines")
    return {"input": Path(path).name, "passed": not failures, "failures": failures,
            "page_count": len(reader.pages), "page_dimensions_mm": list(page_mm),
            "patterns": observed, "reference_lines": references,
            "pdf_numeric_tolerance_points": PDF_POINT_TOLERANCE,
            "pdf_numeric_tolerance_mm": PDF_POINT_TOLERANCE / MM_TO_PT,
            "physical_accuracy_verified": False}


def validate_artifacts(directory: Path, manifest: dict) -> dict:
    config = config_from_dict(manifest["configuration"])
    combined = "_".join(spec.name for spec in config.boards) + "_" + config.printing.page_size.upper() + ".pdf"
    expected_layouts = {f"{spec.name}_charuco.pdf": [p.to_dict() for p in plan_pages(config, (spec,))]
                        for spec in config.boards}
    expected_layouts[combined] = [p.to_dict() for p in plan_pages(config, config.boards)]
    if manifest["pdf_layouts"] != expected_layouts or manifest["combined_pdf"] != combined:
        raise ConfigError("Manifest PDF placements do not match saved configuration")
    expected_files = set(expected_layouts) | {f"{spec.name}_charuco.png" for spec in config.boards}
    if set(manifest["sha256"]) != expected_files:
        raise ConfigError("Manifest checksum file set does not match saved configuration")
    image_reports = []
    pdf_reports = []
    checksum_results = {filename: (directory / filename).is_file() and sha256(directory / filename) == checksum
                        for filename, checksum in manifest["sha256"].items()}
    all_ids = [i for spec in config.boards for i in spec.marker_ids]
    unique = len(all_ids) == len(set(all_ids))
    for record in manifest["boards"]:
        board = reconstruct_board(manifest, record["name"])
        expected_paths = {"png": f"{record['name']}_charuco.png", "pdf": f"{record['name']}_charuco.pdf", "combined_pdf": combined}
        if record["files"] != expected_paths or any(record["sha256"][kind] != manifest["sha256"][filename]
                                                     for kind, filename in expected_paths.items()):
            raise ConfigError("Manifest board paths/checksums are inconsistent")
        # Recalculate raster metrics from the saved config, preventing self-consistent metadata errors.
        if record["raster"] != raster_metrics(config.geometry, config.printing):
            raise ConfigError("Manifest raster metrics do not match saved configuration")
        image_reports.append(validate_image(directory / record["files"]["png"], record, board, synthetic=True))
    for filename, placements in manifest["pdf_layouts"].items():
        try:
            pdf_reports.append(validate_pdf(directory / filename, manifest, placements))
        except (OSError, ValueError, KeyError, PdfReadError) as exc:
            pdf_reports.append({"input": filename, "passed": False, "failures": [f"Cannot verify PDF: {exc}"]})
    passed = unique and all(checksum_results.values()) and all(r["passed"] for r in image_reports + pdf_reports)
    return {"schema_version": 1, "passed": passed, "opencv_version": cv2.__version__,
            "marker_id_sets_disjoint": unique, "checksums_match": checksum_results,
            "boards": image_reports, "pdfs": pdf_reports,
            "physical_accuracy_verified": False, "physical_accuracy_note": PHYSICAL_NOTE}
