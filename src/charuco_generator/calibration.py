"""Standard robot camera calibration target: one ChArUco board filling a printable page.

Camera intrinsic and hand-eye calibration (OpenCV ``calibrateCameraCharuco``, MoveIt Calibration
and similar tools) want the largest ChArUco board a sheet can hold, in a dictionary of its own so
it can never be decoded as a localization reference. This module only chooses that geometry. The
board is generated, verified and photo-validated by the ordinary ChArUco pipeline, so it gets the
same manifest, vector-PDF checks, scale bars and stale-file protection as every other board.
"""
import math
from dataclasses import asdict, dataclass

from .config import Config, ConfigError, Geometry, Printing, config_from_dict, page_size_mm
from .render import single_sheet_pattern_limit
from .selection import assign_ids

BOARD_NAME = "calibration"
#: OpenCV's ChArUco tutorial dictionary. It differs from the DICT_5X5_100 localization references,
#: so a calibration board left in view can never be decoded as Front, Rear or a tank label.
DEFAULT_DICTIONARY = "DICT_6X6_250"
#: On A4 portrait with the default margins this fills the sheet with the conventional 5 x 7 grid.
DEFAULT_SQUARE_MM = 30.0
MARKER_RATIO = 0.75
#: Fewer than 3 x 3 squares leaves fewer than four internal corners, too few for a pose.
MIN_SQUARES = 3


@dataclass(frozen=True)
class CalibrationTarget:
    page_size: str = "A4"
    landscape: bool = False
    square_length_mm: float = DEFAULT_SQUARE_MM
    #: Defaults to MARKER_RATIO x square.
    marker_length_mm: float | None = None
    #: Explicit (columns, rows); by default the largest grid that fits one sheet.
    squares: tuple[int, int] | None = None
    dictionary: str = DEFAULT_DICTIONARY
    first_id: int = 0
    dpi: int = 600
    white_margin_mm: float = 10.0
    page_margin_mm: float = 10.0


def oriented_page(page_size: str, landscape: bool) -> str:
    """The page as given, or its long side horizontal when landscape is requested."""
    width, height = page_size_mm(page_size)
    if not landscape:
        return page_size
    return f"{max(width, height):g}x{min(width, height):g}"


def calibration_geometry(target: CalibrationTarget, printing: Printing) -> Geometry:
    square = target.square_length_mm
    if type(square) not in (int, float) or not math.isfinite(square) or square <= 0:
        raise ConfigError("square_length_mm must be a finite positive number")
    limit_w, limit_h = single_sheet_pattern_limit(printing)
    if target.squares is None:
        squares = (min(100, math.floor(limit_w / square + 1e-9)), min(100, math.floor(limit_h / square + 1e-9)))
        if min(squares) < MIN_SQUARES:
            raise ConfigError(
                f"{square:g} mm squares fit only a {squares[0]} x {squares[1]} grid on a {printing.page_size} sheet; "
                f"calibration needs at least {MIN_SQUARES} x {MIN_SQUARES}. Use smaller squares, a larger page "
                "or smaller margins")
    else:
        squares = target.squares
        if min(squares) < MIN_SQUARES:
            raise ConfigError(f"Calibration grid must be at least {MIN_SQUARES} x {MIN_SQUARES} squares")
        width, height = squares[0] * square, squares[1] * square
        if width > limit_w + 1e-9 or height > limit_h + 1e-9:
            raise ConfigError(
                f"A {squares[0]} x {squares[1]} grid of {square:g} mm squares ({width:g} x {height:g} mm) exceeds "
                f"the {limit_w:g} x {limit_h:g} mm one {printing.page_size} sheet holds with its margins and labels. "
                "Omit --squares to fill the page, use smaller squares, --landscape or a larger page")
    marker = target.marker_length_mm if target.marker_length_mm is not None else round(square * MARKER_RATIO, 6)
    geometry = Geometry(target.dictionary, squares[0], squares[1], float(square), float(marker), False, 1)
    geometry.validate()
    return geometry


def calibration_config(target: CalibrationTarget, output_dir: str = "output/calibration") -> Config:
    """A one-board configuration for the ordinary ChArUco generator."""
    printing = Printing(target.dpi, oriented_page(target.page_size, target.landscape),
                        target.white_margin_mm, target.page_margin_mm)
    printing.validate()
    geometry = calibration_geometry(target, printing)
    data = {"schema_version": 1, "geometry": asdict(geometry), "printing": asdict(printing),
            "output_dir": output_dir, "boards": [{"name": BOARD_NAME, "identifier": BOARD_NAME, "marker_ids": []}]}
    # Consecutive IDs from first_id, with a deterministic identifier for this geometry and ID set.
    assign_ids(data, target.first_id)
    return config_from_dict(data)


def opencv_board_code(config: Config) -> str:
    """The constructor call that rebuilds this board in a calibration program."""
    geometry = config.geometry
    ids = config.boards[0].marker_ids
    # OpenCV numbers markers from 0 by default; any other start must be passed explicitly.
    id_argument = "" if ids[0] == 0 else f", np.arange({ids[0]}, {ids[-1] + 1}, dtype=np.int32)"
    return (f"cv2.aruco.CharucoBoard(({geometry.squares_x}, {geometry.squares_y}), "
            f"{geometry.square_length_mm / 1000:g}, {geometry.marker_length_mm / 1000:g}, "
            f"cv2.aruco.getPredefinedDictionary(cv2.aruco.{geometry.dictionary}){id_argument})")
