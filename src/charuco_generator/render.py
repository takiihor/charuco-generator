"""Lossless rasters and exact-size vector print forms; no fit-to-page path."""
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from reportlab.lib.units import mm

from . import __version__
from .config import BoardSpec, Config, ConfigError, Geometry, Printing, page_size_mm
from .sheet import (
    EDGE_MARK_MM,
    FOOTER_MM,
    HEADER_MM,
    LABEL_BAND_MM,
    MIN_BOARD_PANEL_MM,
    PANEL_GAP_MM,
    REFERENCE_BAND_MM,
    REFERENCE_LINE_MM,
    SCALE_GUTTER_MM,
    Placement,
    draw_scale_marks,
    fit_text,
    new_canvas,
    page_footer,
    page_header,
    vertical_line_length,
)

MEASURE_NOTE = "Check dimensions after printing and mounting. Image detection does not verify physical accuracy."


def raster_metrics(geometry: Geometry, printing: Printing) -> dict:
    ideal = geometry.square_length_mm * printing.dpi / 25.4
    square_px = math.floor(ideal + 0.5)
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
            px * 25.4 / printing.dpi - size for px, size in zip(pattern_px, geometry.pattern_mm, strict=True)],
        "pixel_pitch_mm_at_exact_size": geometry.square_length_mm / square_px,
        "raster_edge_quantization_bound_mm": geometry.square_length_mm / square_px,
        "notes": "Whole pixels per square preserve exact aspect ratio. Raster marker/module edges have pixel "
                 "quantization. PNG pHYs metadata adds integer pixels/metre rounding. "
                 "PDF uses independent exact-size vectors.",
    }


def render_png(path: Path, geometry: Geometry, printing: Printing, board: object) -> dict:
    metrics = raster_metrics(geometry, printing)
    pattern = board.generateImage(tuple(metrics["pattern_pixels"]), marginSize=0,  # type: ignore[attr-defined]
                                  borderBits=geometry.border_bits)
    margin = metrics["margin_pixels"]
    image = cv2.copyMakeBorder(pattern, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=(255,))
    # Mode L avoids palette, antialiasing, JPEG, and colour management surprises.
    Image.fromarray(image).save(path, format="PNG", compress_level=9,
                                dpi=(metrics["png_metadata_dpi"],) * 2)
    return metrics


def pattern_rectangles_mm(geometry: Geometry, spec: BoardSpec, board: object) -> list[tuple[float, float, float, float]]:
    """Black vector rectangles in PDF bottom-left coordinates, from encoded dictionary bits."""
    square = geometry.square_length_mm
    marker = geometry.marker_length_mm
    height = geometry.pattern_mm[1]
    cells: dict[tuple[int, int], int] = {}
    for marker_id, points in zip(spec.marker_ids, board.getObjPoints(), strict=True):  # type: ignore[attr-defined]
        center = np.asarray(points).mean(axis=0) * 1000.0 / square
        cells[(round(float(center[0] - 0.5)), round(float(center[1] - 0.5)))] = marker_id
    rectangles: list[tuple[float, float, float, float]] = []
    dictionary = board.getDictionary()  # type: ignore[attr-defined]
    modules = dictionary.markerSize + 2 * geometry.border_bits
    pitch = marker / modules
    inset = (square - marker) / 2.0
    for row in range(geometry.squares_y):
        for column in range(geometry.squares_x):
            if (column, row) not in cells:
                rectangles.append((column * square, height - (row + 1) * square, square, square))
                continue
            bits = dictionary.generateImageMarker(cells[(column, row)], modules, borderBits=geometry.border_bits)
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


def panel_size(config: Config) -> tuple[float, float]:
    """Panel footprint including the white margin, both reserved bands and the scale gutter."""
    width, height = config.geometry.pattern_mm
    margin = config.printing.white_margin_mm
    panel_w = max(width + 2 * margin, MIN_BOARD_PANEL_MM) + SCALE_GUTTER_MM
    return panel_w, height + 2 * margin + LABEL_BAND_MM + REFERENCE_BAND_MM


def _tile_subrects(pattern_mm: tuple[float, float], tile_mm: tuple[float, float],
                   overlap_mm: float) -> list[tuple[float, float, float, float]]:
    """Board-space sub-rectangles (x0, y0, x1, y1) in mm from the pattern's top-left corner."""
    width, height = pattern_mm
    step_x, step_y = tile_mm[0] - overlap_mm, tile_mm[1] - overlap_mm
    if step_x <= 0 or step_y <= 0:
        raise ConfigError("Tile overlap must be smaller than the printable tile size")
    columns = max(1, math.ceil((width - overlap_mm) / step_x))
    rows = max(1, math.ceil((height - overlap_mm) / step_y))
    result: list[tuple[float, float, float, float]] = []
    for row in range(rows):
        y0 = min(row * step_y, height - tile_mm[1])
        y1 = min(y0 + tile_mm[1], height)
        for column in range(columns):
            x0 = min(column * step_x, width - tile_mm[0])
            result.append((x0, y0, min(x0 + tile_mm[0], width), y1))
    return result


def plan_pages(config: Config, specs: tuple[BoardSpec, ...]) -> list[Placement]:
    """Keep requested size; add pages, then tiles when needed. Never shrink a pattern."""
    page_w, page_h = page_size_mm(config.printing.page_size)
    margin = config.printing.white_margin_mm
    page_margin = config.printing.page_margin_mm
    overlap = config.printing.tile_overlap_mm
    width, height = config.geometry.pattern_mm
    panel_w, panel_h = panel_size(config)
    available_w = page_w - 2 * page_margin
    available_h = page_h - 2 * page_margin - HEADER_MM - FOOTER_MM
    if panel_w > available_w or panel_h > available_h:
        if overlap <= 0:
            raise ConfigError(
                f"WARNING: Board and reserved margins/labels ({panel_w:g} x {panel_h:g} mm) "
                f"exceed printable page area ({available_w:g} x {available_h:g} mm). "
                "Select a larger page or smaller geometry/margins, or split the board across sheets "
                "with --tile-overlap-mm N (assembly seams add error; a single rigid sheet is preferred). "
                "Automatic scaling is forbidden.")
        # Tile the pattern itself: each sheet carries one tile with its own full panel.
        tile_w = available_w - (panel_w - width)
        tile_h = available_h - (panel_h - height)
        if tile_w <= overlap or tile_h <= overlap:
            raise ConfigError(f"No printable tile remains for a {page_w:g} x {page_h:g} mm page; "
                              "reduce margins/labels or use a larger page")
        subrects = _tile_subrects(config.geometry.pattern_mm, (tile_w, tile_h), overlap)
        placements: list[Placement] = []
        for spec_index, spec in enumerate(specs):
            for tile_index, (x0, y0, x1, y1) in enumerate(subrects):
                page = spec_index * len(subrects) + tile_index
                tile_panel_w = max((x1 - x0) + 2 * margin, MIN_BOARD_PANEL_MM) + SCALE_GUTTER_MM
                tile_panel_h = (y1 - y0) + 2 * margin + LABEL_BAND_MM + REFERENCE_BAND_MM
                panel_x, panel_y = (page_w - tile_panel_w) / 2, page_margin + FOOTER_MM + (available_h - tile_panel_h) / 2
                placements.append(Placement(
                    spec.name, page, panel_x + (tile_panel_w - SCALE_GUTTER_MM - (x1 - x0)) / 2,
                    panel_y + REFERENCE_BAND_MM + margin, x1 - x0, y1 - y0,
                    (panel_x, panel_y, tile_panel_w, tile_panel_h),
                    reference_x_mm=min(REFERENCE_LINE_MM, (x1 - x0)), reference_y_mm=vertical_line_length(
                        tile_panel_h - REFERENCE_BAND_MM - LABEL_BAND_MM),
                    form_size=(width, height),
                    subrect_mm=(x0, y0, x1, y1),
                    tile=f"{tile_index + 1}/{len(subrects)}"))
        return placements
    per_page = max(1, math.floor((available_h + PANEL_GAP_MM) // (panel_h + PANEL_GAP_MM)))
    reference_y = vertical_line_length(panel_h - REFERENCE_BAND_MM - LABEL_BAND_MM)
    result: list[Placement] = []
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
                                width, height, (panel_x, panel_y, panel_w, panel_h),
                                reference_y_mm=reference_y))
    return result


def render_pdf(path: Path, config: Config, specs: tuple[BoardSpec, ...], boards: dict) -> list[dict]:
    placements = plan_pages(config, specs)
    page_w, page_h = page_size_mm(config.printing.page_size)
    pdf = new_canvas(path, (page_w, page_h), "ChArUco - actual size localization references",
                     f"charuco-generator {__version__}")
    width, height = config.geometry.pattern_mm
    for spec in specs:
        pdf.beginForm(f"Charuco_{spec.name}", 0, 0, width * mm, height * mm)
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(0, 0, width * mm, height * mm, fill=1, stroke=0)
        pdf.setFillColorRGB(0, 0, 0)
        for x, y, w, h in pattern_rectangles_mm(config.geometry, spec, boards[spec.name]):
            pdf.rect(x * mm, y * mm, w * mm, h * mm, fill=1, stroke=0)
        pdf.endForm()
    by_name = {b.name: b for b in specs}
    geometry = config.geometry
    for page_index in range(max(p.page for p in placements) + 1):
        page_margin = config.printing.page_margin_mm
        page_header(pdf, page_w, page_h, page_margin, "ChArUco localization references")
        for placement in (p for p in placements if p.page == page_index):
            spec = by_name[placement.name]
            px, py, pw, ph = placement.panel_bbox_mm
            label_x = px + 1
            available = (pw - 2) * mm
            pdf.setFillColorRGB(0, 0, 0)
            tile_note = f" | tile {placement.tile}" if placement.tile else ""
            fit_text(pdf, label_x, py + ph - 4,
                     f"{spec.name.upper()} | {spec.identifier}{tile_note}", available, 10)
            fit_text(pdf, label_x, py + ph - 8,
                     f"Pattern {geometry.pattern_mm[0]:g} x {geometry.pattern_mm[1]:g} mm | sheet area "
                     f"{placement.width_mm:g} x {placement.height_mm:g} mm | square {geometry.square_length_mm:g} mm | "
                     f"marker {geometry.marker_length_mm:g} mm", available, 7.5)
            id_text = (",".join(map(str, spec.marker_ids)) if len(spec.marker_ids) <= 12
                       else f"{len(spec.marker_ids)} unique IDs; see manifest")
            fit_text(pdf, label_x, py + ph - 12, f"{geometry.dictionary} | IDs: {id_text}", available, 7)
            pdf.saveState()
            if placement.subrect_mm is not None:
                x0, _y0, x1, y1 = placement.subrect_mm
                clip = pdf.beginPath()
                clip.rect(placement.x_mm * mm, placement.y_mm * mm, (x1 - x0) * mm, placement.height_mm * mm)
                pdf.clipPath(clip, stroke=0, fill=0)
                pdf.translate((placement.x_mm - x0) * mm, (placement.y_mm - (height - y1)) * mm)
            else:
                pdf.translate(placement.x_mm * mm, placement.y_mm * mm)
            pdf.doForm(f"Charuco_{spec.name}")
            pdf.restoreState()
            draw_scale_marks(pdf, placement.panel_bbox_mm,
                             (placement.x_mm, placement.y_mm, placement.width_mm, placement.height_mm),
                             placement.reference_x_mm, placement.reference_y_mm)
            fit_text(pdf, px + 1, py + 2,
                     f"{placement.reference_x_mm:g} mm and {placement.reference_y_mm:g} mm bars, "
                     f"{EDGE_MARK_MM:g} mm edge marks - measure between tick centres", available, 7)
        page_footer(pdf, page_w, page_margin, MEASURE_NOTE)
        pdf.showPage()
    pdf.save()
    return [p.to_dict() for p in placements]
