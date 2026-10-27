"""Standalone ArUco markers with lossless rasters and actual-size vector PDFs."""
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PIL import Image
from pypdf.errors import PdfReadError
from reportlab.lib.units import mm

from . import __version__
from .board import canonical_hash, checksum_mismatch, environment, sha256, untested_opencv_warning
from .config import (
    ConfigError,
    Printing,
    dictionary_capacity,
    dictionary_for,
    dictionary_quality,
    page_size_mm,
)
from .export import VECTOR_DIR, export_dictionary, export_dxf, export_svg
from .generator import clear_stale, write_json
from .selection import ids_label
from .sheet import (
    ACTUAL_SIZE_NOTE,
    FOOTER_MM,
    HEADER_MM,
    LABEL_BAND_MM,
    MIN_MARKER_PANEL_MM,
    PANEL_GAP_MM,
    REFERENCE_BAND_MM,
    REFERENCE_LINE_MM,
    SCALE_GUTTER_MM,
    Placement,
    combined_pdf_name,
    draw_scale_marks,
    fit_text,
    new_canvas,
    page_footer,
    page_header,
    publish,
    vertical_line_length,
)
from .validation import (
    PHYSICAL_NOTE,
    UNEXPECTED_NOTE,
    ValidationError,
    load_manifest,
    quality_check,
    unexpected_files,
    validate_vector_pdf,
)

#: The default set that started this project: tank labels 1..20.
DEFAULT_MARKER_IDS = tuple(range(1, 21))
#: Keeps a single run inside a reasonable number of sheets and files.
MAX_MARKER_IDS = 500
MEASURE_MARKER_NOTE = "Measure printed dimensions and keep the white margin around each marker free of ink."


@dataclass(frozen=True)
class ArucoConfig:
    dictionary: str = "DICT_5X5_100"
    marker_length_mm: float = 30.0
    border_bits: int = 1
    printing: Printing = field(default_factory=Printing)
    marker_ids: tuple[int, ...] = DEFAULT_MARKER_IDS

    def validate(self) -> None:
        self.printing.validate()
        if (type(self.marker_length_mm) not in (int, float)
                or not math.isfinite(self.marker_length_mm) or self.marker_length_mm <= 0):
            raise ConfigError("marker_length_mm must be a finite positive number")
        if type(self.border_bits) is not int or not 1 <= self.border_bits <= 8:
            raise ConfigError("border_bits must be an integer in [1, 8]")
        marker_ids = tuple(self.marker_ids)
        if not marker_ids:
            raise ConfigError("At least one marker ID is required")
        if len(marker_ids) > MAX_MARKER_IDS:
            raise ConfigError(f"Too many markers: {len(marker_ids)} requested, at most {MAX_MARKER_IDS}")
        if any(type(marker_id) is not int for marker_id in marker_ids):
            raise ConfigError("Marker IDs must be integers")
        duplicates = sorted({i for i in marker_ids if marker_ids.count(i) > 1})
        if duplicates:
            raise ConfigError(f"Duplicate marker IDs: {duplicates}")
        capacity = dictionary_capacity(self.dictionary)
        outside = [i for i in marker_ids if i < 0 or i >= capacity]
        if outside:
            raise ConfigError(
                f"{self.dictionary} contains IDs 0..{capacity - 1}, so {outside} cannot be printed. "
                "Lower the IDs or choose a larger dictionary")

    def to_dict(self) -> dict:
        return asdict(self)


def config_from_dict(data: dict) -> ArucoConfig:
    """Rebuild a saved ArUco configuration, rejecting unknown keys and malformed values."""
    if not isinstance(data, dict):
        raise ConfigError("ArUco configuration must be an object")
    extra = set(data) - set(ArucoConfig.__dataclass_fields__)
    if extra:
        raise ConfigError(f"Unknown ArUco configuration keys: {sorted(extra)}")
    try:
        values = dict(data)
        values["printing"] = Printing(**values.get("printing", {}))
        values["marker_ids"] = tuple(values.get("marker_ids", DEFAULT_MARKER_IDS))
        config = ArucoConfig(**values)
    except (TypeError, KeyError) as exc:
        raise ConfigError(f"Malformed ArUco configuration: {exc}") from exc
    config.validate()
    return config


def marker_raster_metrics(config: ArucoConfig) -> dict:
    modules = dictionary_for(config.dictionary).markerSize + 2 * config.border_bits
    ideal = config.marker_length_mm * config.printing.dpi / 25.4
    if not math.isfinite(ideal) or ideal < modules * 4:
        raise ConfigError("Raster marker resolution too low: request at least 4 pixels per marker module")
    module_px = math.floor(ideal / modules + 0.5)
    side_px = modules * module_px
    margin_px = math.ceil(config.printing.white_margin_mm * side_px / config.marker_length_mm)
    image_px = side_px + 2 * margin_px
    if image_px * image_px > 100_000_000:
        raise ConfigError("Raster exceeds the 100 megapixel memory limit; lower DPI or marker size")
    return {"requested_dpi": config.printing.dpi, "modules": modules, "pixels_per_module": module_px,
            "marker_pixels": side_px, "margin_pixels": margin_px,
            "image_pixels": [image_px, image_px],
            "effective_dpi": side_px * 25.4 / config.marker_length_mm,
            "ideal_pixels_per_module": ideal / modules,
            "module_rounding_px": module_px - ideal / modules,
            "white_margin_mm_at_exact_size": margin_px * config.marker_length_mm / side_px,
            "notes": "Whole pixels per module preserve the exact marker aspect ratio; "
                     "the PDF carries independent exact-size vectors."}


def marker_name(marker_id: int) -> str:
    return f"aruco_{marker_id:02d}"


def marker_set_label(marker_ids: tuple[int, ...]) -> str:
    # Only genuinely consecutive runs collapse to a range: 1,2,3,50 is "1-3+50", never "1-50".
    if len(marker_ids) == 1:
        return marker_name(marker_ids[0])
    return f"aruco_{ids_label(marker_ids)}"


def panel_size(config: ArucoConfig) -> tuple[float, float]:
    """Panel footprint with white margin, both reserved bands and the vertical-bar gutter."""
    side = config.marker_length_mm
    margin = config.printing.white_margin_mm
    return (max(side + 2 * margin, MIN_MARKER_PANEL_MM) + SCALE_GUTTER_MM,
            side + 2 * margin + LABEL_BAND_MM + REFERENCE_BAND_MM)


def plan_pages(config: ArucoConfig, marker_ids: tuple[int, ...]) -> list[Placement]:
    """Grid markers across pages at exact size; an oversized marker is an error, never shrunk."""
    page_w, page_h = page_size_mm(config.printing.page_size)
    edge, margin = config.printing.page_margin_mm, config.printing.white_margin_mm
    side = config.marker_length_mm
    panel_w, panel_h = panel_size(config)
    available_w = page_w - 2 * edge
    available_h = page_h - 2 * edge - HEADER_MM - FOOTER_MM
    if panel_w > available_w or panel_h > available_h:
        raise ConfigError(
            f"Marker and reserved margins/labels ({panel_w:g} x {panel_h:g} mm) exceed the printable page area "
            f"({available_w:g} x {available_h:g} mm). Use a larger page, a smaller marker or smaller margins; "
            "automatic scaling is forbidden.")
    columns = min(len(marker_ids), max(1, math.floor((available_w + PANEL_GAP_MM) // (panel_w + PANEL_GAP_MM))))
    rows = max(1, math.floor((available_h + PANEL_GAP_MM) // (panel_h + PANEL_GAP_MM)))
    per_page = columns * rows
    reference_y = vertical_line_length(panel_h - REFERENCE_BAND_MM - LABEL_BAND_MM)
    result: list[Placement] = []
    for index, marker_id in enumerate(marker_ids):
        page, slot = divmod(index, per_page)
        row, column = divmod(slot, columns)
        count = min(per_page, len(marker_ids) - page * per_page)
        used_rows = math.ceil(count / columns)
        group_w = columns * panel_w + (columns - 1) * PANEL_GAP_MM
        group_h = used_rows * panel_h + (used_rows - 1) * PANEL_GAP_MM
        panel_x = (page_w - group_w) / 2 + column * (panel_w + PANEL_GAP_MM)
        panel_y = (edge + FOOTER_MM + (available_h - group_h) / 2
                   + (used_rows - row - 1) * (panel_h + PANEL_GAP_MM))
        result.append(Placement(
            marker_name(marker_id), page,
            panel_x + (panel_w - SCALE_GUTTER_MM - side) / 2,
            panel_y + REFERENCE_BAND_MM + margin, side, side,
            (panel_x, panel_y, panel_w, panel_h),
            reference_x_mm=min(REFERENCE_LINE_MM, side), reference_y_mm=reference_y, form_prefix=""))
    return result


def rectangles(config: ArucoConfig, marker_id: int) -> list[tuple[float, float, float, float]]:
    """Black module squares in PDF bottom-left coordinates, one vector rectangle per module."""
    dictionary = dictionary_for(config.dictionary)
    modules = dictionary.markerSize + 2 * config.border_bits
    bits = dictionary.generateImageMarker(marker_id, modules, borderBits=config.border_bits)
    pitch = config.marker_length_mm / modules
    return [(column * pitch, (modules - row - 1) * pitch, pitch, pitch)
            for row, column in np.argwhere(bits == 0)]


def _render_pdf(path: Path, config: ArucoConfig, marker_ids: tuple[int, ...],
                placements: list[Placement], rects: dict) -> None:
    page_w, page_h = page_size_mm(config.printing.page_size)
    pdf = new_canvas(path, (page_w, page_h),
                     f"ArUco markers {marker_set_label(marker_ids)} - actual size",
                     f"charuco-generator {__version__}")
    side = config.marker_length_mm
    for placement in placements:
        pdf.beginForm(placement.name, 0, 0, side * mm, side * mm)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(0, 0, side * mm, side * mm, fill=1, stroke=0)
        pdf.setFillColorRGB(0, 0, 0)
        for x, y, width, height in rects[placement.name]:
            pdf.rect(x * mm, y * mm, width * mm, height * mm, fill=1, stroke=0)
        pdf.endForm()
    marker_of = {placement.name: next(i for i in marker_ids if marker_name(i) == placement.name)
                 for placement in placements}
    edge = config.printing.page_margin_mm
    for page in range(max(placement.page for placement in placements) + 1):
        page_header(pdf, page_w, page_h, edge,
                    f"ArUco markers | {marker_set_label(marker_ids)} | {len(marker_ids)} markers")
        for placement in (p for p in placements if p.page == page):
            px, py, pw, ph = placement.panel_bbox_mm
            available = (pw - 2) * mm
            pdf.setFillColorRGB(0, 0, 0)
            fit_text(pdf, px + 1, py + ph - 4, f"ID {marker_of[placement.name]} | {side:g} x {side:g} mm", available, 10)
            fit_text(pdf, px + 1, py + ph - 9,
                     f"{config.dictionary} | border {config.border_bits} | {config.printing.page_size.upper()} sheet",
                     available, 7)
            pdf.saveState()
            pdf.translate(placement.x_mm * mm, placement.y_mm * mm)
            pdf.doForm(placement.name)
            pdf.restoreState()
            draw_scale_marks(pdf, placement.panel_bbox_mm,
                             (placement.x_mm, placement.y_mm, placement.width_mm, placement.height_mm),
                             placement.reference_x_mm, placement.reference_y_mm)
            fit_text(pdf, px + 1, py + 2,
                     f"{placement.reference_x_mm:g} mm and {placement.reference_y_mm:g} mm bars - measure tick centres",
                     available, 7)
        page_footer(pdf, page_w, edge, f"{ACTUAL_SIZE_NOTE}. {MEASURE_MARKER_NOTE}")
        pdf.showPage()
    pdf.save()


def pdf_layouts(config: ArucoConfig) -> dict:
    """Every sheet layout, keyed by published filename, derived only from the configuration."""
    marker_ids = tuple(config.marker_ids)
    layouts = {f"{marker_name(marker_id)}.pdf": [p.to_dict() for p in plan_pages(config, (marker_id,))]
               for marker_id in marker_ids}
    combined = combined_pdf_name([marker_set_label(marker_ids)], config.printing.page_size, config.to_dict())
    layouts[combined] = [p.to_dict() for p in plan_pages(config, marker_ids)]
    return layouts


def export_marker_files(directory: Path, config: ArucoConfig, rects: dict, *, svg: bool, dxf: bool) -> list[str]:
    """CAD copies of the marker modules; returns names relative to the artifact directory."""
    if not (svg or dxf):
        return []
    folder = directory / VECTOR_DIR
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, rectangles in rects.items():
        if svg:
            export_svg(folder / f"{name}.svg", (config.marker_length_mm,) * 2, rectangles,
                       f"ArUco {name} {config.dictionary}")
            written.append(f"{VECTOR_DIR}/{name}.svg")
        if dxf:
            export_dxf(folder / f"{name}.dxf", rectangles)
            written.append(f"{VECTOR_DIR}/{name}.dxf")
    return sorted(written)


def validate_aruco_artifacts(directory: Path, manifest: dict) -> dict:
    if manifest.get("kind") != "aruco" or manifest.get("schema_version") != 1:
        raise ConfigError("Not an ArUco manifest; verify ChArUco boards with board_manifest.json")
    config = config_from_dict(manifest["configuration"])
    if manifest["configuration_sha256"] != canonical_hash(config.to_dict()):
        raise checksum_mismatch(manifest)
    metrics = marker_raster_metrics(config)
    layouts = pdf_layouts(config)
    marker_ids = tuple(config.marker_ids)
    if (manifest["marker_ids"] != list(marker_ids) or manifest["raster"] != metrics
            or manifest["pdf_layouts"] != layouts):
        raise ConfigError("Manifest marker IDs, raster metrics or PDF placements differ from the saved configuration")
    exports = sorted(manifest.get("exports", []))
    if any(Path(name).parent != Path(VECTOR_DIR) for name in exports):
        raise ConfigError("Manifest exports must live in the vector/ subdirectory")
    expected_files = (set(layouts) | {f"{marker_name(marker_id)}.png" for marker_id in marker_ids} | set(exports))
    if set(manifest["sha256"]) != expected_files:
        raise ConfigError("Manifest checksum file set does not match the saved configuration")
    checksums = {name: (directory / name).is_file() and sha256(directory / name) == checksum
                 for name, checksum in manifest["sha256"].items()}
    stale = unexpected_files(directory, set(manifest["sha256"]))
    parameters = cv2.aruco.DetectorParameters()
    parameters.markerBorderBits = config.border_bits
    detector = cv2.aruco.ArucoDetector(dictionary_for(config.dictionary), parameters)
    image_reports, pdf_reports = [], []
    for marker_id in marker_ids:
        filename = f"{marker_name(marker_id)}.png"
        failures, detected, image = [], [], None
        try:
            with Image.open(directory / filename) as saved:
                image = np.asarray(saved)
                if saved.mode != "L" or list(saved.size) != metrics["image_pixels"]:
                    failures.append("PNG mode or dimensions differ")
                if not np.allclose(saved.info.get("dpi", (0, 0)), [metrics["effective_dpi"]] * 2, rtol=0, atol=0.03):
                    failures.append("PNG resolution metadata differs")
            if image is None or image.ndim != 2:
                failures.append("PNG must be grayscale")
            else:
                _, ids, _ = detector.detectMarkers(image)
                detected = [] if ids is None else sorted(ids.ravel().tolist())
                margin = metrics["margin_pixels"]
                if (not np.isin(image, [0, 255]).all() or not (image[:margin] == 255).all()
                        or not (image[-margin:] == 255).all() or not (image[:, :margin] == 255).all()
                        or not (image[:, -margin:] == 255).all()):
                    failures.append("PNG must contain only black/white pixels with a clear white margin")
        except (OSError, ValueError, cv2.error) as exc:
            failures.append(f"Cannot verify PNG: {exc}")
        if detected != [marker_id]:
            failures.append(f"Expected only marker ID {marker_id}, detected {detected}")
        image_reports.append({"input": filename, "marker_id": marker_id, "detected_marker_ids": detected,
                              "passed": not failures, "failures": failures})
    rects = {marker_name(marker_id): rectangles(config, marker_id) for marker_id in marker_ids}
    for filename, placements in layouts.items():
        try:
            pdf_reports.append(validate_vector_pdf(directory / filename, page_size_mm(config.printing.page_size),
                                                  placements, rects))
        except (OSError, ValueError, KeyError, PdfReadError) as exc:
            pdf_reports.append({"input": filename, "passed": False, "failures": [f"Cannot verify PDF: {exc}"]})
    quality, quality_failures, warnings = quality_check(manifest, config.dictionary, list(marker_ids))
    passed = (all(checksums.values()) and not stale and not quality_failures
              and all(item["passed"] for item in image_reports + pdf_reports))
    if (warning := untested_opencv_warning()) is not None:
        warnings.append(warning)
    return {"schema_version": 1, "kind": "aruco", "passed": passed,
            "marker_ids": list(marker_ids), "markers": image_reports, "pdfs": pdf_reports,
            "checksums_match": checksums, "unexpected_files": stale, "unexpected_files_note": UNEXPECTED_NOTE,
            "dictionary_quality": quality, "quality_failures": quality_failures, "warnings": warnings,
            "physical_accuracy_verified": False, "physical_accuracy_note": PHYSICAL_NOTE}


def generate_aruco(config: ArucoConfig, output_dir: str | Path = "output/aruco", *, clean: bool = False,
                   svg: bool = False, dxf: bool = False, dictionary_file: bool = False) -> dict:
    config.validate()
    marker_ids = tuple(config.marker_ids)
    layouts = pdf_layouts(config)
    metrics = marker_raster_metrics(config)
    target = Path(output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    clear_stale(target, set(layouts) | {f"{marker_name(i)}.png" for i in marker_ids}, clean)
    dictionary = dictionary_for(config.dictionary)
    with TemporaryDirectory(prefix=".aruco-staging-", dir=target.parent) as staging:
        directory = Path(staging)
        for marker_id in marker_ids:
            marker = dictionary.generateImageMarker(marker_id, metrics["marker_pixels"], borderBits=config.border_bits)
            margin = metrics["margin_pixels"]
            image = cv2.copyMakeBorder(marker, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=(255,))
            Image.fromarray(image).save(directory / f"{marker_name(marker_id)}.png",
                                        format="PNG", compress_level=9, dpi=(metrics["effective_dpi"],) * 2)
        rects = {marker_name(marker_id): rectangles(config, marker_id) for marker_id in marker_ids}
        single_sheets = {f"{marker_name(marker_id)}.pdf": (marker_id,) for marker_id in marker_ids}
        for filename in layouts:
            sheet_ids = single_sheets.get(filename, marker_ids)
            _render_pdf(directory / filename, config, sheet_ids, plan_pages(config, sheet_ids), rects)
        filenames = {f"{marker_name(marker_id)}.png" for marker_id in marker_ids} | set(layouts)
        exports = export_marker_files(directory, config, rects, svg=svg, dxf=dxf)
        if dictionary_file:
            exports.append(export_dictionary(directory / VECTOR_DIR / f"{config.dictionary}.yml", config.dictionary))
        configuration = config.to_dict()
        manifest = {"schema_version": 1, "kind": "aruco", "generator_version": __version__,
                    "environment": environment(), "configuration": configuration,
                    "configuration_sha256": canonical_hash(configuration), "marker_ids": list(marker_ids),
                    "path_convention": "All file paths are relative to the directory containing this manifest",
                    "raster": metrics, "pdf_layouts": layouts, "exports": sorted(exports),
                    "dictionary_quality": dictionary_quality(config.dictionary, list(marker_ids)),
                    "sha256": {name: sha256(directory / name) for name in sorted(filenames | set(exports))}}
        write_json(directory / "aruco_manifest.json", manifest)
        report = validate_aruco_artifacts(directory, load_manifest(directory / "aruco_manifest.json"))
        report["manifest_sha256"] = sha256(directory / "aruco_manifest.json")
        if not report["passed"]:
            raise ValidationError(report)
        write_json(directory / "aruco_validation_report.json", report)
        publish(directory, target, sorted(filenames | set(exports)),
                ["aruco_manifest.json", "aruco_validation_report.json"])
        return report
