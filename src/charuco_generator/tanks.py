"""Tank identification boards: one small ChArUco board per tank, giving identity and position.

Each tank gets its own board named ``tankNN`` with its own block of marker IDs. Seeing any of a
block's IDs identifies the tank; the board's corners give the coordinate. A board must have at
least MIN_CORNERS internal corners, the number charuco-realsense requires before it trusts a pose
(a 3 x 3 board has only 4, so it can never give a trusted coordinate).

IDs come from DICT_6X6_250 starting after the calibration board (IDs 0-16), so tank boards never
share a code with the DICT_5X5_100 Front/Rear references or standalone tank labels. A larger 5x5
dictionary would not help: DICT_5X5_250 and DICT_5X5_1000 begin with the same 100 codes as
DICT_5X5_100. Generation and verification are the ordinary ChArUco pipeline.
"""
import re
from dataclasses import asdict, dataclass

from .config import Config, ConfigError, Geometry, Printing, config_from_dict, dictionary_for
from .selection import assign_ids

DEFAULT_DICTIONARY = "DICT_6X6_250"
#: The A4 calibration board uses IDs 0-16 of the same dictionary.
DEFAULT_FIRST_ID = 17
DEFAULT_SQUARES = (4, 4)
DEFAULT_SQUARE_MM = 20.0
MARKER_RATIO = 0.75
#: charuco-realsense PoseOptions.min_corners: fewer internal corners never pass its quality gate.
MIN_CORNERS = 6
#: Nominal D435i colour focal length at 1280 x 720, for the working-distance estimate only.
D435I_FX_PX = 910.0
#: Below this many image pixels per marker module, detection and corners become unreliable.
MIN_PIXELS_PER_MODULE = 5.0
NAME_PATTERN = re.compile(r"tank(\d+)")


@dataclass(frozen=True)
class TankSetTarget:
    count: int = 20
    first_tank: int = 1
    squares: tuple[int, int] = DEFAULT_SQUARES
    square_length_mm: float = DEFAULT_SQUARE_MM
    #: Defaults to MARKER_RATIO x square.
    marker_length_mm: float | None = None
    dictionary: str = DEFAULT_DICTIONARY
    first_id: int = DEFAULT_FIRST_ID
    dpi: int = 600
    page_size: str = "A4"
    white_margin_mm: float = 10.0
    page_margin_mm: float = 10.0


def tank_name(number: int) -> str:
    return f"tank{number:02d}"


def tank_config(target: TankSetTarget, output_dir: str = "output/tanks") -> Config:
    """One board per tank, consecutive non-overlapping ID blocks in tank order."""
    if type(target.count) is not int or not 1 <= target.count <= 100:
        raise ConfigError("Tank count must be an integer from 1 to 100")
    if type(target.first_tank) is not int or not 1 <= target.first_tank <= 999 - target.count + 1:
        raise ConfigError("First tank number must be a positive integer (tank numbers up to 999)")
    squares = target.squares
    if not isinstance(squares, (tuple, list)) or len(squares) != 2 or any(type(v) is not int for v in squares):
        raise ConfigError("Tank board grid must be two integers, e.g. 4x4")
    corners = (squares[0] - 1) * (squares[1] - 1)
    if corners < MIN_CORNERS:
        raise ConfigError(
            f"A {squares[0]} x {squares[1]} board has {corners} internal corners; a trusted coordinate needs at "
            f"least {MIN_CORNERS} (charuco-realsense rejects poses with fewer). Use 4x3 or larger; 4x4 is the default")
    square = target.square_length_mm
    marker = target.marker_length_mm if target.marker_length_mm is not None else (
        round(square * MARKER_RATIO, 6) if isinstance(square, (int, float)) else square)
    geometry = Geometry(target.dictionary, squares[0], squares[1], square, marker, False, 1)
    geometry.validate()
    printing = Printing(target.dpi, target.page_size, target.white_margin_mm, target.page_margin_mm)
    names = [tank_name(target.first_tank + index) for index in range(target.count)]
    data = {"schema_version": 1, "geometry": asdict(geometry), "printing": asdict(printing), "output_dir": output_dir,
            "boards": [{"name": name, "identifier": name, "marker_ids": []} for name in names]}
    # Consecutive blocks in tank order; also gives each board a deterministic identifier.
    assign_ids(data, target.first_id)
    return config_from_dict(data)


def tank_number(board_name: str) -> int | None:
    match = NAME_PATTERN.fullmatch(board_name)
    return int(match.group(1)) if match else None


def tank_table(config: Config) -> list[dict]:
    """Tank number, board name and marker IDs, in tank order."""
    return [{"tank": tank_number(board.name), "board": board.name, "marker_ids": list(board.marker_ids)}
            for board in config.boards if tank_number(board.name) is not None]


def tank_for_marker_id(manifest: dict, marker_id: int) -> int | None:
    """Which tank a detected marker ID belongs to, from a saved board manifest (None if no tank)."""
    for board in manifest["configuration"]["boards"]:
        if marker_id in board["marker_ids"]:
            return tank_number(board["name"])
    return None


def detection_range(geometry: Geometry, fx_px: float = D435I_FX_PX) -> dict:
    """Marker module size and the farthest camera distance with enough pixels per module."""
    modules = dictionary_for(geometry.dictionary).markerSize + 2 * geometry.border_bits
    module_mm = geometry.marker_length_mm / modules
    return {"module_mm": module_mm,
            "max_distance_mm": module_mm * fx_px / MIN_PIXELS_PER_MODULE,
            "assumption": f"fx {fx_px:g} px (D435i colour 1280x720), {MIN_PIXELS_PER_MODULE:g} px per module"}

