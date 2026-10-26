"""Standalone markers 1..20 with lossless rasters and actual-size vector PDFs."""
from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np
from PIL import Image
from pypdf.errors import PdfReadError
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from . import __version__
from .board import canonical_hash, check_opencv, environment, sha256
from .config import ConfigError, Printing, dictionary_for, page_size_mm
from .generator import write_json
from .render import (FOOTER_MM, HEADER_MM, LABEL_BAND_MM, PANEL_GAP_MM,
                     REFERENCE_BAND_MM, _fitted_text)
from .validation import PHYSICAL_NOTE, ValidationError, load_manifest, validate_vector_pdf

MARKER_IDS = tuple(range(1, 21))


@dataclass(frozen=True)
class ArucoConfig:
    dictionary: str = "DICT_5X5_100"
    marker_length_mm: float = 30.0
    border_bits: int = 1
    printing: Printing = Printing()

    def validate(self) -> None:
        check_opencv()
        self.printing.validate()
        if (type(self.marker_length_mm) not in (int, float)
                or not math.isfinite(self.marker_length_mm) or self.marker_length_mm <= 0):
            raise ConfigError("marker_length_mm must be a finite positive number")
        if type(self.border_bits) is not int or not 1 <= self.border_bits <= 8:
            raise ConfigError("border_bits must be an integer in [1, 8]")
        if dictionary_for(self.dictionary).bytesList.shape[0] <= max(MARKER_IDS):
            raise ConfigError("Dictionary must contain every marker ID from 1 through 20")


def marker_raster_metrics(config: ArucoConfig) -> dict:
    modules = dictionary_for(config.dictionary).markerSize + 2 * config.border_bits
    ideal = config.marker_length_mm * config.printing.dpi / 25.4
    if not math.isfinite(ideal) or ideal < modules * 4:
        raise ConfigError("Raster marker resolution too low: request at least 4 pixels per marker module")
    module_px = int(math.floor(ideal / modules + 0.5))
    side_px = modules * module_px
    margin_px = math.ceil(config.printing.white_margin_mm * side_px / config.marker_length_mm)
    image_px = side_px + 2 * margin_px
    if image_px * image_px > 100_000_000:
        raise ConfigError("Raster exceeds the 100 megapixel memory limit; lower DPI or marker size")
    return {"requested_dpi": config.printing.dpi, "pixels_per_module": module_px,
            "marker_pixels": side_px, "margin_pixels": margin_px,
            "image_pixels": [image_px, image_px],
            "effective_dpi": side_px * 25.4 / config.marker_length_mm,
            "white_margin_mm_at_exact_size": margin_px * config.marker_length_mm / side_px}


def _name(marker_id: int) -> str:
    return f"aruco_{marker_id:02d}"


def _layout(config: ArucoConfig, ids: tuple[int, ...]) -> list[dict]:
    page_w, page_h = page_size_mm(config.printing.page_size)
    edge, margin = config.printing.page_margin_mm, config.printing.white_margin_mm
    side = config.marker_length_mm
    panel_w = max(side + 2 * margin, 60.0)
    panel_h = side + 2 * margin + LABEL_BAND_MM + REFERENCE_BAND_MM
    available_w = page_w - 2 * edge
    available_h = page_h - 2 * edge - HEADER_MM - FOOTER_MM
    if panel_w > available_w or panel_h > available_h:
        raise ConfigError("Marker and reserved margins/labels exceed printable page area; use a larger page or smaller marker")
    columns = min(len(ids), max(1, int((available_w + PANEL_GAP_MM + 1e-9) // (panel_w + PANEL_GAP_MM))))
    rows = max(1, int((available_h + PANEL_GAP_MM + 1e-9) // (panel_h + PANEL_GAP_MM)))
    per_page = columns * rows
    result = []
    for index, marker_id in enumerate(ids):
        page, slot = divmod(index, per_page)
        row, column = divmod(slot, columns)
        count = min(per_page, len(ids) - page * per_page)
        used_rows = math.ceil(count / columns)
        group_w = columns * panel_w + (columns - 1) * PANEL_GAP_MM
        group_h = used_rows * panel_h + (used_rows - 1) * PANEL_GAP_MM
        panel_x = (page_w - group_w) / 2 + column * (panel_w + PANEL_GAP_MM)
        panel_y = edge + FOOTER_MM + (available_h - group_h) / 2 + (used_rows - row - 1) * (panel_h + PANEL_GAP_MM)
        name = _name(marker_id)
        result.append({"board": name, "page": page, "pdf_form": f"/FormXob.{name}",
                       "pattern_bbox_mm_xywh": [panel_x + (panel_w - side) / 2,
                                                panel_y + REFERENCE_BAND_MM + margin, side, side],
                       "panel_bbox_mm_xywh": [panel_x, panel_y, panel_w, panel_h],
                       "reference_line_mm": 50.0})
    return result


def _rectangles(config: ArucoConfig, marker_id: int) -> list[tuple[float, float, float, float]]:
    dictionary = dictionary_for(config.dictionary)
    modules = dictionary.markerSize + 2 * config.border_bits
    bits = dictionary.generateImageMarker(marker_id, modules, borderBits=config.border_bits)
    pitch = config.marker_length_mm / modules
    return [(column * pitch, (modules - row - 1) * pitch, pitch, pitch)
            for row, column in np.argwhere(bits == 0)]


def _render_pdf(path: Path, config: ArucoConfig, placements: list[dict], rectangles: dict) -> None:
    page_w, page_h = page_size_mm(config.printing.page_size)
    pdf = canvas.Canvas(str(path), pagesize=(page_w * mm, page_h * mm), pageCompression=1, invariant=1)
    pdf.setTitle("ArUco markers 1-20 - actual size")
    pdf.setAuthor("Precision ChArUco Generator")
    pdf.setViewerPreference("PrintScaling", "None")
    side = config.marker_length_mm
    for placement in placements:
        name = placement["board"]
        pdf.beginForm(name, 0, 0, side * mm, side * mm)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(0, 0, side * mm, side * mm, fill=1, stroke=0)
        pdf.setFillColorRGB(0, 0, 0)
        for x, y, width, height in rectangles[name]:
            pdf.rect(x * mm, y * mm, width * mm, height * mm, fill=1, stroke=0)
        pdf.endForm()
    for page in range(max(item["page"] for item in placements) + 1):
        edge = config.printing.page_margin_mm
        available = (page_w - 2 * edge) * mm
        _fitted_text(pdf, edge, page_h - edge - 4, "ArUco markers | IDs 1-20", available, 12)
        _fitted_text(pdf, edge, page_h - edge - 9, "ACTUAL SIZE / 100% - disable Fit, Shrink and printer scaling", available, 8)
        for placement in (item for item in placements if item["page"] == page):
            name = placement["board"]
            x, y, _, _ = placement["pattern_bbox_mm_xywh"]
            px, py, pw, ph = placement["panel_bbox_mm_xywh"]
            _fitted_text(pdf, px + 1, py + ph - 4, f"ID {int(name.removeprefix('aruco_'))} | {side:g} x {side:g} mm", (pw - 2) * mm, 10)
            _fitted_text(pdf, px + 1, py + ph - 9, f"{config.dictionary} | border {config.border_bits}", (pw - 2) * mm, 7)
            pdf.saveState()
            pdf.translate(x * mm, y * mm)
            pdf.doForm(name)
            pdf.restoreState()
            line_x, line_y = px + (pw - 50) / 2, py + 6
            pdf.setLineWidth(0.2 * mm)
            pdf.line(line_x * mm, line_y * mm, (line_x + 50) * mm, line_y * mm)
            for end in (line_x, line_x + 50):
                pdf.line(end * mm, (line_y - 1) * mm, end * mm, (line_y + 1) * mm)
            _fitted_text(pdf, line_x, py + 2, "50 mm - tick centres", 50 * mm, 7)
        _fitted_text(pdf, edge, edge + 2, "Measure printed dimensions. Keep the white margin around each marker.", available, 7)
        pdf.showPage()
    pdf.save()


def _pdf_layouts(config: ArucoConfig) -> dict:
    layouts = {f"{_name(marker_id)}.pdf": _layout(config, (marker_id,)) for marker_id in MARKER_IDS}
    layouts[f"aruco_1-20_{config.printing.page_size.upper()}.pdf"] = _layout(config, MARKER_IDS)
    return layouts


def validate_aruco_artifacts(directory: Path, manifest: dict) -> dict:
    if manifest.get("kind") != "aruco" or manifest.get("schema_version") != 1:
        raise ConfigError("Unsupported ArUco manifest schema")
    try:
        data = dict(manifest["configuration"])
        data["printing"] = Printing(**data["printing"])
        config = ArucoConfig(**data)
    except (TypeError, KeyError) as exc:
        raise ConfigError(f"Malformed ArUco configuration: {exc}") from exc
    config.validate()
    if manifest["configuration_sha256"] != canonical_hash(asdict(config)):
        raise ConfigError("Manifest configuration checksum mismatch")
    metrics = marker_raster_metrics(config)
    layouts = _pdf_layouts(config)
    if manifest["marker_ids"] != list(MARKER_IDS) or manifest["raster"] != metrics or manifest["pdf_layouts"] != layouts:
        raise ConfigError("Manifest marker IDs, raster or PDF placements differ from saved configuration")
    expected_files = set(layouts) | {f"{_name(marker_id)}.png" for marker_id in MARKER_IDS}
    if set(manifest["sha256"]) != expected_files:
        raise ConfigError("Manifest checksum file set does not match saved configuration")
    checksums = {name: (directory / name).is_file() and sha256(directory / name) == checksum
                 for name, checksum in manifest["sha256"].items()}
    parameters = cv2.aruco.DetectorParameters()
    parameters.markerBorderBits = config.border_bits
    detector = cv2.aruco.ArucoDetector(dictionary_for(config.dictionary), parameters)
    image_reports, pdf_reports = [], []
    for marker_id in MARKER_IDS:
        filename = f"{_name(marker_id)}.png"
        failures, detected = [], []
        try:
            with Image.open(directory / filename) as saved:
                image = np.asarray(saved)
                if saved.mode != "L" or list(saved.size) != metrics["image_pixels"]:
                    failures.append("PNG mode or dimensions differ")
                if not np.allclose(saved.info.get("dpi", (0, 0)), [metrics["effective_dpi"]] * 2, rtol=0, atol=0.03):
                    failures.append("PNG resolution metadata differs")
            if image.ndim != 2:
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
    rectangles = {_name(marker_id): _rectangles(config, marker_id) for marker_id in MARKER_IDS}
    for filename, placements in layouts.items():
        try:
            pdf_reports.append(validate_vector_pdf(directory / filename, page_size_mm(config.printing.page_size), placements, rectangles))
        except (OSError, ValueError, KeyError, PdfReadError) as exc:
            pdf_reports.append({"input": filename, "passed": False, "failures": [f"Cannot verify PDF: {exc}"]})
    passed = all(checksums.values()) and all(item["passed"] for item in image_reports + pdf_reports)
    return {"schema_version": 1, "kind": "aruco", "passed": passed,
            "marker_ids": list(MARKER_IDS), "markers": image_reports, "pdfs": pdf_reports,
            "checksums_match": checksums, "physical_accuracy_verified": False, "physical_accuracy_note": PHYSICAL_NOTE}


def generate_aruco(config: ArucoConfig, output_dir: str | Path = "output/aruco") -> dict:
    config.validate()
    layouts = _pdf_layouts(config)
    metrics = marker_raster_metrics(config)
    target = Path(output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    dictionary = dictionary_for(config.dictionary)
    with TemporaryDirectory(prefix=".aruco-staging-", dir=target.parent) as staging:
        directory = Path(staging)
        for marker_id in MARKER_IDS:
            marker = dictionary.generateImageMarker(marker_id, metrics["marker_pixels"], borderBits=config.border_bits)
            image = cv2.copyMakeBorder(marker, *([metrics["margin_pixels"]] * 4), cv2.BORDER_CONSTANT, value=255)
            Image.fromarray(image).save(directory / f"{_name(marker_id)}.png", compress_level=9, dpi=(metrics["effective_dpi"],) * 2)
        rectangles = {_name(marker_id): _rectangles(config, marker_id) for marker_id in MARKER_IDS}
        for filename, placements in layouts.items():
            _render_pdf(directory / filename, config, placements, rectangles)
        filenames = {f"{_name(marker_id)}.png" for marker_id in MARKER_IDS} | set(layouts)
        manifest = {"schema_version": 1, "kind": "aruco", "generator_version": __version__,
                    "environment": environment(), "configuration": asdict(config),
                    "configuration_sha256": canonical_hash(asdict(config)), "marker_ids": list(MARKER_IDS),
                    "path_convention": "All file paths are relative to the directory containing this manifest",
                    "raster": metrics, "pdf_layouts": layouts,
                    "sha256": {name: sha256(directory / name) for name in sorted(filenames)}}
        write_json(directory / "aruco_manifest.json", manifest)
        report = validate_aruco_artifacts(directory, load_manifest(directory / "aruco_manifest.json"))
        report["manifest_sha256"] = sha256(directory / "aruco_manifest.json")
        if not report["passed"]:
            raise ValidationError(report)
        write_json(directory / "aruco_validation_report.json", report)
        target.mkdir(parents=True, exist_ok=True)
        for filename in [*sorted(filenames), "aruco_manifest.json", "aruco_validation_report.json"]:
            os.replace(directory / filename, target / filename)
        return report
