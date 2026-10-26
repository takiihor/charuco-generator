"""Lossless rasters and exact-size vector print forms; no fit-to-page path."""
from dataclasses import dataclass
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

from .config import BoardSpec, Config, ConfigError, Geometry, Printing, page_size_mm

LABEL_BAND_MM = 14.0
REFERENCE_BAND_MM = 10.0
HEADER_MM = 14.0
FOOTER_MM = 8.0
PANEL_GAP_MM = 6.0


def raster_metrics(geometry: Geometry, printing: Printing) -> dict:
    ideal = geometry.square_length_mm * printing.dpi / 25.4
    square_px = int(math.floor(ideal + 0.5))
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, geometry.dictionary))
    modules = dictionary.markerSize + 2 * geometry.border_bits
    if square_px * geometry.marker_length_mm / geometry.square_length_mm < modules * 4:
        raise ConfigError("Raster marker resolution too low: request at least 4 pixels per marker module")
    pattern_px = [geometry.squares_x * square_px, geometry.squares_y * square_px]
    effective_dpi = square_px * 25.4 / geometry.square_length_mm
    margin_px = math.ceil(printing.white_margin_mm * square_px / geometry.square_length_mm)
    image_px = [v + 2 * margin_px for v in pattern_px]
    if image_px[0] * image_px[1] > 100_000_000:
        raise ConfigError("Raster exceeds the 100 megapixel memory limit; lower DPI or board size")
    return {
        "requested_dpi": printing.dpi, "pixels_per_square": square_px,
        "ideal_pixels_per_square": ideal, "square_rounding_px": square_px - ideal,
        "pattern_pixels": pattern_px, "margin_pixels": margin_px, "image_pixels": image_px,
        "pattern_bbox_pixels_xywh": [margin_px, margin_px, *pattern_px],
        "effective_pattern_dpi_at_exact_size": effective_dpi,
        "png_metadata_dpi": effective_dpi,
        "white_margin_mm_at_exact_size": margin_px * geometry.square_length_mm / square_px,
        "pattern_mm_if_forced_to_requested_dpi": [v * 25.4 / printing.dpi for v in pattern_px],
        "pattern_rounding_mm_at_requested_dpi": [
            px * 25.4 / printing.dpi - size for px, size in zip(pattern_px, geometry.pattern_mm)],
        "pixel_pitch_mm_at_exact_size": geometry.square_length_mm / square_px,
        "raster_edge_quantization_bound_mm": geometry.square_length_mm / square_px,
        "notes": "Whole pixels per square preserve exact aspect ratio. Raster marker/module edges have pixel quantization. PNG pHYs metadata adds integer pixels/metre rounding. PDF uses independent exact-size vectors.",
    }


def render_png(path: Path, geometry: Geometry, printing: Printing, board) -> dict:
    metrics = raster_metrics(geometry, printing)
    pattern = board.generateImage(tuple(metrics["pattern_pixels"]), marginSize=0,
                                  borderBits=geometry.border_bits)
    image = cv2.copyMakeBorder(pattern, *([metrics["margin_pixels"]] * 4),
                               cv2.BORDER_CONSTANT, value=255)
    # Mode L avoids palette, antialiasing, JPEG, and colour management surprises.
    Image.fromarray(image).save(path, format="PNG", compress_level=9,
                               dpi=(metrics["png_metadata_dpi"],) * 2)
    return metrics


def pattern_rectangles_mm(geometry: Geometry, spec: BoardSpec, board) -> list[tuple[float, float, float, float]]:
    """Black vector rectangles in PDF bottom-left coordinates, from encoded dictionary bits."""
    square = geometry.square_length_mm
    marker = geometry.marker_length_mm
    height = geometry.pattern_mm[1]
    cells = {}
    for marker_id, points in zip(spec.marker_ids, board.getObjPoints(), strict=True):
        center = np.asarray(points).mean(axis=0) * 1000.0 / square
        cells[(int(round(float(center[0] - 0.5))), int(round(float(center[1] - 0.5))))] = marker_id
    rectangles = []
    modules = board.getDictionary().markerSize + 2 * geometry.border_bits
    pitch = marker / modules
    inset = (square - marker) / 2.0
    for row in range(geometry.squares_y):
        for column in range(geometry.squares_x):
            if (column, row) not in cells:
                rectangles.append((column * square, height - (row + 1) * square, square, square))
                continue
            bits = board.getDictionary().generateImageMarker(cells[(column, row)], modules,
                                                              borderBits=geometry.border_bits)
            # Merge horizontal runs, keeping all vector module boundaries exact in mm.
            for by in range(modules):
                bx = 0
                while bx < modules:
                    if bits[by, bx] != 0:
                        bx += 1
                        continue
                    start = bx
                    while bx < modules and bits[by, bx] == 0:
                        bx += 1
                    rectangles.append((column * square + inset + start * pitch,
                                       height - (row * square + inset + (by + 1) * pitch),
                                       (bx - start) * pitch, pitch))
    return rectangles


@dataclass(frozen=True)
class Placement:
    name: str
    page: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    panel_bbox_mm: tuple[float, float, float, float]

    def to_dict(self) -> dict:
        return {"board": self.name, "page": self.page,
                "pdf_form": f"/FormXob.Charuco_{self.name}",
                "pattern_bbox_mm_xywh": [self.x_mm, self.y_mm, self.width_mm, self.height_mm],
                "panel_bbox_mm_xywh": list(self.panel_bbox_mm),
                "reference_line_mm": 50.0}


def plan_pages(config: Config, specs: tuple[BoardSpec, ...]) -> list[Placement]:
    """Keep requested size; add pages when needed, never shrink a pattern."""
    page_w, page_h = page_size_mm(config.printing.page_size)
    margin = config.printing.white_margin_mm
    page_margin = config.printing.page_margin_mm
    width, height = config.geometry.pattern_mm
    panel_w = max(width + 2 * margin, 110.0)
    panel_h = height + 2 * margin + LABEL_BAND_MM + REFERENCE_BAND_MM
    available_w = page_w - 2 * page_margin
    available_h = page_h - 2 * page_margin - HEADER_MM - FOOTER_MM
    if panel_w > available_w or panel_h > available_h:
        raise ConfigError(
            f"WARNING: Board and reserved margins/labels ({panel_w:g} x {panel_h:g} mm) "
            f"exceed printable page area ({available_w:g} x {available_h:g} mm). "
            "Select a larger page or smaller geometry/margins; automatic scaling is forbidden.")
    per_page = max(1, int((available_h + PANEL_GAP_MM + 1e-9) // (panel_h + PANEL_GAP_MM)))
    result = []
    for index, spec in enumerate(specs):
        page = index // per_page
        slot = index % per_page
        count = min(per_page, len(specs) - page * per_page)
        group_height = count * panel_h + (count - 1) * PANEL_GAP_MM
        group_bottom = page_margin + FOOTER_MM + (available_h - group_height) / 2
        panel_y = group_bottom + (count - 1 - slot) * (panel_h + PANEL_GAP_MM)
        panel_x = (page_w - panel_w) / 2
        result.append(Placement(spec.name, page, (page_w - width) / 2,
                                panel_y + REFERENCE_BAND_MM + margin,
                                width, height, (panel_x, panel_y, panel_w, panel_h)))
    return result


def _fitted_text(pdf, x: float, y: float, text: str, max_width: float, size: float) -> None:
    measured = stringWidth(text, "Helvetica", size)
    if measured > max_width:
        size *= max_width / measured
    pdf.setFont("Helvetica", size)
    pdf.drawString(x * mm, y * mm, text)


def render_pdf(path: Path, config: Config, specs: tuple[BoardSpec, ...], boards: dict) -> list[dict]:
    placements = plan_pages(config, specs)
    page_w, page_h = page_size_mm(config.printing.page_size)
    pdf = canvas.Canvas(str(path), pagesize=(page_w * mm, page_h * mm),
                        pageCompression=1, invariant=1)
    pdf.setTitle("ChArUco - actual size localization references")
    pdf.setAuthor("Precision ChArUco Generator")
    pdf.setCreator("charuco-generator 1.0.0")
    pdf.setViewerPreference("PrintScaling", "None")
    for spec in specs:
        width, height = config.geometry.pattern_mm
        pdf.beginForm(f"Charuco_{spec.name}", 0, 0, width * mm, height * mm)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(0, 0, width * mm, height * mm, fill=1, stroke=0)
        pdf.setFillColorRGB(0, 0, 0)
        for x, y, w, h in pattern_rectangles_mm(config.geometry, spec, boards[spec.name]):
            pdf.rect(x * mm, y * mm, w * mm, h * mm, fill=1, stroke=0)
        pdf.endForm()
    by_name = {b.name: b for b in specs}
    for page_index in range(max(p.page for p in placements) + 1):
        pm = config.printing.page_margin_mm
        _fitted_text(pdf, pm, page_h - pm - 4, "ChArUco localization references", (page_w - 2 * pm) * mm, 12)
        _fitted_text(pdf, pm, page_h - pm - 9,
                     "ACTUAL SIZE / 100% - disable Fit, Shrink and printer scaling", (page_w - 2 * pm) * mm, 8)
        for placement in (p for p in placements if p.page == page_index):
            spec = by_name[placement.name]
            px, py, pw, ph = placement.panel_bbox_mm
            label_x = px + 1
            available = (pw - 2) * mm
            pdf.setFillColorRGB(0, 0, 0)
            _fitted_text(pdf, label_x, py + ph - 4, f"{spec.name.upper()} | {spec.identifier}", available, 10)
            g = config.geometry
            _fitted_text(pdf, label_x, py + ph - 8,
                         f"Pattern {placement.width_mm:g} x {placement.height_mm:g} mm | square {g.square_length_mm:g} mm | marker {g.marker_length_mm:g} mm", available, 7.5)
            id_text = ",".join(map(str, spec.marker_ids)) if len(spec.marker_ids) <= 12 else f"{len(spec.marker_ids)} unique IDs; see manifest"
            _fitted_text(pdf, label_x, py + ph - 12, f"{g.dictionary} | IDs: {id_text}", available, 7)
            pdf.saveState()
            pdf.translate(placement.x_mm * mm, placement.y_mm * mm)
            pdf.doForm(f"Charuco_{spec.name}")
            pdf.restoreState()
            # All reference ink stays below the reserved white margin.
            line_x = placement.x_mm
            line_y = py + 6
            pdf.setLineWidth(0.2 * mm)
            pdf.line(line_x * mm, line_y * mm, (line_x + 50) * mm, line_y * mm)
            for end in (line_x, line_x + 50):
                pdf.line(end * mm, (line_y - 1) * mm, end * mm, (line_y + 1) * mm)
            _fitted_text(pdf, line_x, py + 2, "50 mm - measure between tick centres", available, 7)
        _fitted_text(pdf, pm, pm + 2, "Check dimensions after printing and mounting. Image detection does not verify physical accuracy.",
                     (page_w - 2 * pm) * mm, 7)
        pdf.showPage()
    pdf.save()
    return [p.to_dict() for p in placements]
