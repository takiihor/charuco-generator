"""Independent file readback, modern detectors, and PDF content inspection."""
import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import ContentStream

from . import __version__
from .board import reconstruct_board, sha256, untested_opencv_warning
from .config import (
    Config,
    ConfigError,
    Geometry,
    config_from_dict,
    dictionary_quality,
    page_size_mm,
)
from .render import pattern_rectangles_mm, plan_pages, raster_metrics
from .sheet import VECTOR_DIR, combined_pdf_name

MM_TO_PT = 72.0 / 25.4
PDF_POINT_TOLERANCE = 0.001  # <= 0.000353 mm; accommodates ReportLab number serialization up to 2 m pages.
REFERENCE_TOLERANCE_MM = 0.005  # matches printed scale bars to within 5 micrometres of nominal length.
PHYSICAL_NOTE = ("Image detection and PDF geometry checks do not establish printed dimensions or robot placement "
                 "accuracy. Measure the physical print independently.")
#: Below this many image pixels per marker module, corner localisation and decoding stop being
#: reliable, so a photograph that "works" at this resolution should not be trusted.
MIN_PIXELS_PER_MODULE = 5.0
#: Shared by both artifact kinds, so a leftover file always carries the same remedy.
UNEXPECTED_NOTE = ("Files matching this list were not produced by the saved configuration; "
                   "delete them or regenerate with --clean.")

#: CLI name -> (DetectorParameters attribute, low, high). Rates are relative to the image size or
#: marker size, so they stay meaningful at any resolution; window sizes are pixel counts.
DETECTOR_OVERRIDES = {
    "min_perimeter_rate": ("minMarkerPerimeterRate", 0.001, 0.5),
    "max_perimeter_rate": ("maxMarkerPerimeterRate", 0.001, 4.0),
    "adaptive_thresh_window": ("adaptiveThreshWinSizeMin", 1, 2000),
    "min_marker_distance_rate": ("minMarkerDistanceRate", 0.0, 4.0),
    "polygonal_approx_accuracy_rate": ("polygonalApproxAccuracyRate", 0.0, 0.5),
}
CORNER_REFINEMENTS = {"none": "CORNER_REFINE_NONE", "subpix": "CORNER_REFINE_SUBPIX",
                      "contour": "CORNER_REFINE_CONTOUR"}


def detector_parameters(geometry: Geometry, overrides: dict | None = None):
    """Build detector settings from the board geometry plus any CLI-tuned overrides.

    Corner refinement stays off by default: synthetic checks compare detected pixels against the
    exact board layout, and refinement would blur that comparison. Photographs can opt in.
    """
    params = cv2.aruco.DetectorParameters()
    params.markerBorderBits = geometry.border_bits
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_NONE
    overrides = overrides or {}
    for name, value in overrides.items():
        if value is None:
            continue
        if name == "corner_refinement":
            if value not in CORNER_REFINEMENTS:
                raise ConfigError(f"Unknown corner refinement method: {value}")
            params.cornerRefinementMethod = getattr(cv2.aruco, CORNER_REFINEMENTS[value])
            continue
        if name not in DETECTOR_OVERRIDES:
            raise ConfigError(f"Unknown detector parameter: {name}")
        attribute, low, high = DETECTOR_OVERRIDES[name]
        if not math.isfinite(value) or not low <= value <= high:
            raise ConfigError(f"{name} must be a finite number in [{low}, {high}], got {value}")
        setattr(params, attribute, int(value) if name == "adaptive_thresh_window" else float(value))
    return params


class ValidationError(RuntimeError):
    def __init__(self, report: dict):
        self.report = report
        super().__init__("Generated artifacts failed validation; inspect the validation report")


#: Keys every verifier dereferences, per manifest kind. Checked at load so a truncated or
#: hand-edited file cannot surface later as a bare ``error: 'boards'``.
MANIFEST_KEYS = {
    "charuco": ("schema_version", "generator_version", "environment", "configuration", "configuration_sha256",
                "boards", "combined_pdf", "pdf_layouts", "sha256"),
    "aruco": ("schema_version", "generator_version", "environment", "configuration", "configuration_sha256",
              "marker_ids", "pdf_layouts", "sha256"),
}


def manifest_kind(manifest: dict) -> str:
    return manifest.get("kind", "charuco")


def load_manifest(path: str | Path) -> dict:
    location = Path(path)
    try:
        manifest = json.loads(location.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {location}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Cannot read manifest {location}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ConfigError(f"{location} must contain a JSON object, found {type(manifest).__name__}")
    kind = manifest_kind(manifest)
    if kind not in MANIFEST_KEYS:
        raise ConfigError(f"{location} has unknown manifest kind {kind!r}; expected "
                          f"{' or '.join(sorted(MANIFEST_KEYS))}")
    missing = [key for key in MANIFEST_KEYS[kind] if key not in manifest]
    if missing:
        raise ConfigError(f"{kind} manifest {location} is missing required key(s): {', '.join(missing)}. "
                          "Regenerate the artifacts instead of editing the manifest.")
    return manifest


def _ids(values) -> list[int]:
    return [] if values is None else np.asarray(values).ravel().astype(int).tolist()


def _pixels_per_module(board, border_bits: int, marker_corners) -> dict:
    """Sampled resolution of each decoded marker, measured in image pixels per marker module."""
    modules = board.getDictionary().markerSize + 2 * border_bits
    sides = [float(np.mean([np.linalg.norm(point[i] - point[(i + 1) % 4]) for i in range(4)]))
             for points in marker_corners for point in [np.asarray(points).reshape(-1, 2)[:4]]]
    if not sides:
        return {"markers_measured": 0, "modules_per_marker": modules}
    values = [side / modules for side in sides]
    return {"markers_measured": len(values), "modules_per_marker": modules,
            "min_pixels_per_module": round(min(values), 3), "mean_pixels_per_module": round(sum(values) / len(values), 3),
            "mean_marker_side_px": round(sum(sides) / len(sides), 2)}


def validate_image(path: str | Path, record: dict, board, *, synthetic: bool = False,
                   require_complete: bool = False, detector_overrides: dict | None = None) -> dict:
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
    params = detector_parameters(geometry, detector_overrides)
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
    corner_points = np.empty((0, 2)) if charuco_corners is None else charuco_corners.reshape(-1, 2)
    report.update(
        image_pixels=[image.shape[1], image.shape[0]], expected_marker_ids=expected,
        detected_marker_ids=sorted(detected), detected_marker_count=len(detected),
        matched_marker_ids=own, missing_marker_ids=missing, foreign_marker_ids=foreign,
        rejected_marker_candidate_count=len(rejected), detected_charuco_ids=sorted(corner_ids),
        detected_charuco_corner_count=len(corner_ids), expected_charuco_corner_count=geometry.corner_count,
        full_expected_visibility=full_visibility,
        charuco_image_points=[{"id": corner_id, "pixel_xy": point.tolist()}
                              for corner_id, point in zip(corner_ids, corner_points, strict=False)],
    )
    if not synthetic:
        quality = _pixels_per_module(board, geometry.border_bits, marker_corners)
        warnings = []
        if quality.get("markers_measured") and quality["min_pixels_per_module"] < MIN_PIXELS_PER_MODULE:
            warnings.append(
                f"Markers sample to {quality['min_pixels_per_module']:g} pixels per module (below "
                f"{MIN_PIXELS_PER_MODULE:g}); corners are under-resolved, so IDs and geometry are unreliable. "
                "Move closer, increase resolution or use larger squares")
        report["detection_quality"] = quality
        report["warnings"] = warnings
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
        for corner_id, point in zip(corner_ids, corner_points, strict=False):
            expected_point = board.getChessboardCorners()[corner_id, :2] * pitch + margin - 0.5
            corner_errors.append(float(np.linalg.norm(point - expected_point)))
        centers = {marker_id: np.asarray(points).mean(axis=0)[:2] * pitch + margin - 0.5
                   for marker_id, points in zip(expected, board.getObjPoints(), strict=True)}
        marker_errors = [float(np.linalg.norm(np.asarray(points).reshape(-1, 2).mean(axis=0) - centers[marker_id]))
                         for marker_id, points in zip(detected, marker_corners, strict=False) if marker_id in centers]
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


def _matrix(values: Sequence[Any]) -> np.ndarray:
    a, b, c, d, e, f = map(float, values)
    return np.array([[a, c, e], [b, d, f], [0, 0, 1]])


def validate_pdf(path: str | Path, manifest: dict, expected_placements: list[dict]) -> dict:
    """Inspect actual MediaBox, form rectangles, BBox and placement CTMs, not metadata claims."""
    config = config_from_dict(manifest["configuration"])
    rectangles = {spec.name: pattern_rectangles_mm(config.geometry, spec, reconstruct_board(manifest, spec.name))
                  for spec in config.boards}
    return validate_vector_pdf(path, page_size_mm(config.printing.page_size), expected_placements, rectangles)


def _wanted_placement_matrix(placement: dict) -> np.ndarray:
    """Expected CTM of a form on the page, including the tile offset for split sheets."""
    x, y = placement["pattern_bbox_mm_xywh"][0], placement["pattern_bbox_mm_xywh"][1]
    shift_x, shift_y = 0.0, 0.0
    if "subrect_mm" in placement:
        x0, _y0, _x1, y1 = placement["subrect_mm"]
        shift_x = -x0
        shift_y = -(placement["form_size_mm"][1] - y1)
    return _matrix([1, 0, 0, 1, (x + shift_x) * MM_TO_PT, (y + shift_y) * MM_TO_PT])


def validate_vector_pdf(path: str | Path, page_mm: tuple[float, float],
                        expected_placements: list[dict], rectangles: dict) -> dict:
    """Check physical PDF geometry for either boards or standalone marker forms.

    Every panel must carry both reference bars: measuring the horizontal and the vertical bar
    separately is what reveals anisotropic printer or paper scaling.
    """
    reader = PdfReader(path)
    failures = []
    observed = []
    references = []
    expected_pages = max(p["page"] for p in expected_placements) + 1
    if len(reader.pages) != expected_pages:
        failures.append("Unexpected PDF page count")
    prefs: Any = reader.trailer["/Root"]
    if hasattr(prefs, "get"):
        prefs = prefs.get("/ViewerPreferences", {})
        if hasattr(prefs, "get_object"):
            prefs = prefs.get_object()
    if prefs.get("/PrintScaling") != "/None":
        failures.append("PDF does not request PrintScaling=None")
    # Tiled sheets replay one shared form on several pages, so a form name alone does not identify
    # the placement it must match; the page index completes the key.
    by_form = {(item["pdf_form"], item["page"]): item for item in expected_placements}
    if len(by_form) != len(expected_placements):
        raise ConfigError("Two placements claim the same PDF form on the same page")
    # ReportLab serialises coordinates with limited digits, so bars are matched within a
    # tolerance instead of by exact value. The tolerance is ~100x smaller than the smallest
    # difference between two printable bar lengths.
    wanted_bars: dict[tuple[int, str], list[float]] = {}
    for placement in expected_placements:
        for bar_axis, key in (("horizontal", "reference_line_mm"), ("vertical", "reference_line_y_mm")):
            wanted_bars.setdefault((placement["page"], bar_axis), []).append(float(placement[key]))
    for page_index, page in enumerate(reader.pages):
        actual_box = list(map(float, page.mediabox))
        target_box = [0, 0, page_mm[0] * MM_TO_PT, page_mm[1] * MM_TO_PT]
        if not np.allclose(actual_box, target_box, rtol=0, atol=PDF_POINT_TOLERANCE):
            failures.append(f"Page {page_index}: physical page size differs")
        if list(page.cropbox) != list(page.mediabox) or page.rotation != 0 or float(page.get("/UserUnit", 1)) != 1:
            failures.append(f"Page {page_index}: unexpected crop, rotation or user unit")
        ctm = np.eye(3)
        stack: list[np.ndarray] = []
        current_point: np.ndarray | None = None
        resources: Any = page["/Resources"]
        xobjects = resources.get("/XObject", {})
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
                length_mm = float(np.linalg.norm(end[:2] - current_point[:2]) / MM_TO_PT)
                axis: str | None = None
                if abs(end[1] - current_point[1]) < 0.0001:
                    axis = "horizontal"
                elif abs(end[0] - current_point[0]) < 0.0001:
                    axis = "vertical"
                if axis is not None:
                    remaining = wanted_bars.get((page_index, axis), [])
                    for position, candidate in enumerate(remaining):
                        if abs(length_mm - candidate) <= REFERENCE_TOLERANCE_MM:
                            del remaining[position]
                            references.append({"page": page_index, "axis": axis, "length_mm": length_mm,
                                               "nominal_length_mm": candidate,
                                               "from_xy_mm": [float(current_point[0] / MM_TO_PT),
                                                              float(current_point[1] / MM_TO_PT)]})
                            break
                current_point = end
            elif operation == b"Do":
                name = str(operands[0])
                expected = by_form.get((name, page_index))
                if expected is None:
                    failures.append(f"Unexpected PDF drawing object: {name}")
                    continue
                form = xobjects[name].get_object()
                transform = ctm @ _matrix(form.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                bbox = np.array(list(map(float, form["/BBox"])))
                form_width, form_height = expected["form_size_mm"]
                if not np.allclose(transform, _wanted_placement_matrix(expected), rtol=0, atol=PDF_POINT_TOLERANCE):
                    failures.append(f"{name}: placement transform changed (scaling, translation or distortion)")
                if not np.allclose(
                        bbox, [0, 0, form_width * MM_TO_PT, form_height * MM_TO_PT], rtol=0, atol=PDF_POINT_TOLERANCE):
                    failures.append(f"{name}: pattern bounding box differs from requested physical dimensions")
                # Also check every actual pattern rectangle, including marker modules.
                rects = [[float(v) for v in args] for args, op in ContentStream(form, reader).operations if op == b"re"]
                expected_rects = [[0, 0, form_width * MM_TO_PT, form_height * MM_TO_PT]] + [
                    [v * MM_TO_PT for v in rect] for rect in rectangles[expected["board"]]]
                if len(rects) != len(expected_rects) or not np.allclose(rects, expected_rects, rtol=0, atol=PDF_POINT_TOLERANCE):
                    failures.append(f"{name}: vector pattern or marker module geometry differs")
                origin = transform @ np.array([bbox[0], bbox[1], 1])
                opposite = transform @ np.array([bbox[2], bbox[3], 1])
                entry = {"board": expected["board"], "page": page_index,
                         "pattern_bbox_mm_xywh": expected["pattern_bbox_mm_xywh"],
                         "transformed_form_bbox_mm_xywh": [float(v / MM_TO_PT) for v in
                                                           [origin[0], origin[1], opposite[0] - origin[0], opposite[1] - origin[1]]],
                         "vector_rectangle_count": len(rects)}
                if "tile" in expected:
                    entry["tile"] = expected["tile"]
                    entry["subrect_mm"] = expected["subrect_mm"]
                observed.append(entry)
    if sorted((p["board"], p["page"]) for p in observed) != sorted((p["board"], p["page"]) for p in expected_placements):
        failures.append("Missing or duplicate board forms")
    if any(remaining for remaining in wanted_bars.values()):
        missing = [f"page {page} {axis} {length:g} mm"
                   for (page, axis), lengths in sorted(wanted_bars.items()) for length in lengths]
        failures.append(f"Missing or incorrect reference bars: {', '.join(missing)}")
    return {"input": Path(path).name, "passed": not failures, "failures": failures,
            "page_count": len(reader.pages), "page_dimensions_mm": list(page_mm),
            "patterns": observed, "reference_lines": sorted(references, key=lambda r: (r["page"], r["axis"], r["from_xy_mm"])),
            "pdf_numeric_tolerance_points": PDF_POINT_TOLERANCE,
            "pdf_numeric_tolerance_mm": PDF_POINT_TOLERANCE / MM_TO_PT,
            "physical_accuracy_verified": False}


ARTIFACT_SUFFIXES = {".png", ".pdf"}


def unexpected_files(directory: Path, known: set[str]) -> list[str]:
    """Artifact-shaped files in a directory that the current configuration does not produce.

    A leftover PNG or PDF from an earlier configuration is the dangerous case: it can be picked
    up, printed and mistaken for a board whose geometry was verified, while it actually encodes
    a different grid, ID set or page layout.
    """
    if not directory.is_dir():
        return []
    return sorted(path.name for path in directory.iterdir()
                  if path.is_file() and path.suffix.lower() in ARTIFACT_SUFFIXES and path.name not in known)


def quality_check(manifest: dict, dictionary: str, marker_ids: list[int]) -> tuple[dict, list[str], list[str]]:
    """Recompute dictionary quality; return (quality, failures, warnings).

    Generator 1.0.0 recorded distances with a bit-unpacking error, so a mismatch in a manifest
    from another generator version is reported as a warning: the artifacts are still valid.
    """
    quality = dictionary_quality(dictionary, marker_ids)
    saved = manifest.get("dictionary_quality")
    if saved in (None, quality):
        return quality, [], []
    if manifest.get("generator_version") != __version__:
        return quality, [], [f"Dictionary quality recorded by generator {manifest.get('generator_version')} used an "
                             "earlier formula; the recomputed values are reported here"]
    return quality, ["Manifest dictionary quality differs from the saved configuration"], []


def expected_artifacts(config: Config) -> set[str]:
    """Artifact filenames a board configuration produces, known before anything is rendered."""
    names = {f"{spec.name}_charuco{suffix}" for spec in config.boards for suffix in (".png", ".pdf")}
    names.add(combined_pdf_name([spec.name for spec in config.boards],
                                 config.printing.page_size, config.to_dict()))
    return names


def validate_artifacts(directory: Path, manifest: dict) -> dict:
    config = config_from_dict(manifest["configuration"])
    combined = combined_pdf_name([spec.name for spec in config.boards],
                                 config.printing.page_size, config.to_dict())
    expected_layouts = {f"{spec.name}_charuco.pdf": [p.to_dict() for p in plan_pages(config, (spec,))]
                        for spec in config.boards}
    expected_layouts[combined] = [p.to_dict() for p in plan_pages(config, config.boards)]
    if manifest["pdf_layouts"] != expected_layouts or manifest["combined_pdf"] != combined:
        raise ConfigError("Manifest PDF placements do not match saved configuration")
    exports = sorted(manifest.get("exports", []))
    if any(Path(name).parent != Path(VECTOR_DIR) for name in exports):
        raise ConfigError("Manifest exports must live in the vector/ subdirectory")
    if set(manifest["sha256"]) != expected_artifacts(config) | set(exports):
        raise ConfigError("Manifest checksum file set does not match saved configuration")
    image_reports = []
    pdf_reports = []
    stale = unexpected_files(directory, set(manifest["sha256"]))
    checksum_results = {filename: (directory / filename).is_file() and sha256(directory / filename) == checksum
                        for filename, checksum in manifest["sha256"].items()}
    all_ids = [i for spec in config.boards for i in spec.marker_ids]
    unique = len(all_ids) == len(set(all_ids))
    quality, quality_failures, warnings = quality_check(manifest, config.geometry.dictionary, all_ids)
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
    passed = (unique and not stale and not quality_failures
              and all(checksum_results.values()) and all(r["passed"] for r in image_reports + pdf_reports))
    if (warning := untested_opencv_warning()) is not None:
        warnings.append(warning)
    return {"schema_version": 1, "passed": passed, "opencv_version": cv2.__version__,
            "marker_id_sets_disjoint": unique, "checksums_match": checksum_results,
            "unexpected_files": stale, "unexpected_files_note": UNEXPECTED_NOTE,
            "dictionary_quality": quality,
            "quality_failures": quality_failures, "warnings": warnings,
            "boards": image_reports, "pdfs": pdf_reports,
            "physical_accuracy_verified": False, "physical_accuracy_note": PHYSICAL_NOTE}
