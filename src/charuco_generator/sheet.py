"""Shared print-sheet geometry: bands, gutters, actual-size marks and publication.

Both the ChArUco and the standalone ArUco sheets are built from these helpers so that a
formatting change cannot land on one path only. All lengths are millimetres unless a name
ends in ``_pt`` (PDF points).
"""
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .config import ConfigError

LABEL_BAND_MM = 14.0
REFERENCE_BAND_MM = 10.0
HEADER_MM = 14.0
FOOTER_MM = 8.0
PANEL_GAP_MM = 6.0
#: Length of the horizontal scale bar drawn in every panel's reference band.
REFERENCE_LINE_MM = 50.0
#: Vertical scale bars use the largest of these that fits with clearance.
REFERENCE_LINE_LENGTHS_MM = (50.0, 40.0, 30.0, 25.0, 20.0)
#: Room to the right of each panel holding the vertical scale bar, clear of the white margin.
SCALE_GUTTER_MM = 14.0
#: Minimum panel width so labels and the horizontal scale bar stay readable.
MIN_BOARD_PANEL_MM = 110.0
MIN_MARKER_PANEL_MM = 60.0
#: Clearance between a scale bar and the panel's reserved bands.
SCALE_BAR_CLEARANCE_MM = 2.0
#: Shortest vertical scale bar worth printing; shorter bars cannot be measured reliably.
MIN_REFERENCE_BAR_MM = 10.0
MARK_LINE_WIDTH_MM = 0.2
TICK_HALF_LENGTH_MM = 1.0
#: Length of the short ticks that project the pattern's left/right edges into both bands. They sit
#: in the strip next to the white margin, clear of the label lines and the bar caption.
EDGE_MARK_MM = 2.5
#: Gap between an edge mark and the white margin, and height of the horizontal bar above the panel
#: bottom: its ticks stay above the caption (baseline 2 mm) and below the edge marks.
EDGE_MARK_GAP_MM = 1.0
HORIZONTAL_BAR_Y_MM = 5.5
#: Combined sheet names are derived from board names; long sets fall back to a digest so
#: the published filename stays well inside the 255 byte filesystem limit.
MAX_COMBINED_NAME_CHARS = 180
#: Subdirectory of an artifact set that holds CAD/dictionary exports, kept apart from print files.
VECTOR_DIR = "vector"
ACTUAL_SIZE_NOTE = "ACTUAL SIZE / 100% - disable Fit, Shrink and printer scaling"


def fit_text(pdf: canvas.Canvas, x: float, y: float, text: str, max_width_pt: float, size: float) -> None:
    """Draw text, reducing the font size uniformly so it never leaves its band."""
    measured = stringWidth(text, "Helvetica", size)
    if 0 < max_width_pt < measured:
        size *= max_width_pt / measured
    pdf.setFont("Helvetica", size)
    pdf.drawString(x * mm, y * mm, text)


def vertical_line_length(usable_height_mm: float) -> float:
    """Largest standard scale bar that fits with clearance, shrinking to the space available.

    A board short enough to leave no room for even the minimum bar cannot be printed with a
    usable vertical reference, so it is rejected with the same guidance as an oversized page.
    Whole millimetres keep the printed annotation readable on a ruler.
    """
    for candidate in REFERENCE_LINE_LENGTHS_MM:
        if candidate + 2 * SCALE_BAR_CLEARANCE_MM <= usable_height_mm:
            return candidate
    fallback = math.floor(usable_height_mm - 2 * SCALE_BAR_CLEARANCE_MM)
    if fallback < MIN_REFERENCE_BAR_MM:
        raise ConfigError(f"Panel height {usable_height_mm + 2 * SCALE_BAR_CLEARANCE_MM:g} mm leaves no room for a "
                          f"vertical scale bar of at least {MIN_REFERENCE_BAR_MM:g} mm; "
                          "increase the pattern size or the white margin")
    return float(fallback)


def draw_scale_marks(pdf: canvas.Canvas, panel: tuple[float, float, float, float],
                     pattern: tuple[float, float, float, float], horizontal_mm: float,
                     vertical_mm: float) -> None:
    """Draw the actual-size bars and the pattern-edge projection marks of one panel.

    Everything stays outside the pattern and its white margin: the horizontal bar lives in the
    reference band below the margin, the vertical bar in the right-hand gutter, and the short
    projection ticks line up with the pattern's left and right edges so a straightedge can
    transfer the trim line / mounting datum without guessing where the pattern corner is.
    Measuring both bars is what exposes anisotropic print scaling.
    """
    px, py, pw, ph = panel
    x0, _y0, width, _height = pattern
    x1 = x0 + width
    pdf.setLineWidth(MARK_LINE_WIDTH_MM * mm)
    line_x = px + (pw - SCALE_GUTTER_MM - horizontal_mm) / 2.0
    line_y = py + HORIZONTAL_BAR_Y_MM
    pdf.line(line_x * mm, line_y * mm, (line_x + horizontal_mm) * mm, line_y * mm)
    for end in (line_x, line_x + horizontal_mm):
        pdf.line(end * mm, (line_y - TICK_HALF_LENGTH_MM) * mm, end * mm, (line_y + TICK_HALF_LENGTH_MM) * mm)
    usable_height = ph - REFERENCE_BAND_MM - LABEL_BAND_MM
    vertical_x = px + pw - SCALE_GUTTER_MM / 2.0
    vertical_y = py + REFERENCE_BAND_MM + (usable_height - vertical_mm) / 2.0
    pdf.line(vertical_x * mm, vertical_y * mm, vertical_x * mm, (vertical_y + vertical_mm) * mm)
    for end in (vertical_y, vertical_y + vertical_mm):
        pdf.line((vertical_x - TICK_HALF_LENGTH_MM) * mm, end * mm,
                 (vertical_x + TICK_HALF_LENGTH_MM) * mm, end * mm)
    bottom_mark = py + REFERENCE_BAND_MM - EDGE_MARK_GAP_MM
    top_mark = py + ph - LABEL_BAND_MM + EDGE_MARK_GAP_MM
    for edge in (x0, x1):
        pdf.line(edge * mm, (bottom_mark - EDGE_MARK_MM) * mm, edge * mm, bottom_mark * mm)
        pdf.line(edge * mm, top_mark * mm, edge * mm, (top_mark + EDGE_MARK_MM) * mm)


@dataclass(frozen=True)
class Placement:
    """Where one pattern (or one pattern tile) is placed on one physical page."""
    name: str
    page: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    panel_bbox_mm: tuple[float, float, float, float]
    reference_x_mm: float = REFERENCE_LINE_MM
    reference_y_mm: float = REFERENCE_LINE_MM
    form_prefix: str = "Charuco_"
    #: Full pattern extent of the vector form when a sheet clips only part of it (tiling).
    form_size: tuple[float, float] | None = None
    #: Board-space sub-rectangle for tiled sheets: (x0, y0, x1, y1) in mm from the pattern origin.
    subrect_mm: tuple[float, float, float, float] | None = None
    tile: str = ""

    @property
    def form_name(self) -> str:
        return f"/FormXob.{self.form_prefix}{self.name}"

    @property
    def form_size_mm(self) -> tuple[float, float]:
        """Full pattern size drawn by the vector form, which one sheet may clip."""
        return (self.width_mm, self.height_mm) if self.form_size is None else self.form_size

    def to_dict(self) -> dict:
        form_width, form_height = self.form_size_mm
        result: dict = {"board": self.name, "page": self.page, "pdf_form": self.form_name,
                        "pattern_bbox_mm_xywh": [self.x_mm, self.y_mm, self.width_mm, self.height_mm],
                        "panel_bbox_mm_xywh": list(self.panel_bbox_mm),
                        "form_size_mm": [form_width, form_height],
                        "reference_line_mm": self.reference_x_mm,
                        "reference_line_y_mm": self.reference_y_mm}
        if self.subrect_mm is not None:
            result["tile"] = self.tile
            result["subrect_mm"] = list(self.subrect_mm)
        return result


def page_header(pdf: canvas.Canvas, page_w: float, page_h: float, edge_mm: float, title: str) -> None:
    available = (page_w - 2 * edge_mm) * mm
    fit_text(pdf, edge_mm, page_h - edge_mm - 4, title, available, 12)
    fit_text(pdf, edge_mm, page_h - edge_mm - 9, ACTUAL_SIZE_NOTE, available, 8)


def page_footer(pdf: canvas.Canvas, page_w: float, edge_mm: float, note: str) -> None:
    fit_text(pdf, edge_mm, edge_mm + 2, note, (page_w - 2 * edge_mm) * mm, 7)


def new_canvas(path: Path, page_mm: tuple[float, float], title: str, creator: str) -> canvas.Canvas:
    pdf = canvas.Canvas(str(path), pagesize=(page_mm[0] * mm, page_mm[1] * mm), pageCompression=1, invariant=1)
    pdf.setTitle(title)
    pdf.setAuthor("Precision ChArUco Generator")
    pdf.setCreator(creator)
    pdf.setViewerPreference("PrintScaling", "None")
    return pdf


def _numbered_run(board_names: list[str]) -> str | None:
    """'tank01-20' for consecutively numbered names sharing one prefix (tank01, tank02, ... tank20)."""
    if len(board_names) < 3:
        return None
    parts = []
    for name in board_names:
        match = re.fullmatch(r"([a-z_]*[a-z_])(\d+)", name)
        if match is None:
            return None
        parts.append((match.group(1), match.group(2)))
    numbers = [int(digits) for _, digits in parts]
    if len({prefix for prefix, _ in parts}) != 1 or numbers != list(range(numbers[0], numbers[0] + len(numbers))):
        return None
    return f"{board_names[0]}-{parts[-1][1]}"


def combined_pdf_name(board_names: list[str], page_size: str, digest_source: object) -> str:
    """Name a multi-board sheet, falling back to a digest when board names overflow."""
    joined = _numbered_run(board_names) or "_".join(board_names)
    if len(joined) > MAX_COMBINED_NAME_CHARS:
        digest = hashlib.sha256(
            json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
        joined = f"boards-{digest}"
    return f"{joined}_{page_size.upper()}.pdf"


def publish(staging: Path, target: Path, filenames: list[str], last_names: list[str]) -> None:
    """Replace artifacts individually, with the manifest and report published last.

    Individual replacements are atomic; this is not a multi-file filesystem transaction, so
    an interrupted set is detected by the manifest checksums when ``verify`` runs.
    """
    for filename in [*sorted(filenames), *last_names]:
        destination = target / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging / filename, destination)
