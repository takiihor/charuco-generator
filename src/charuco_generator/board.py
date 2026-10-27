"""Modern OpenCV board construction, layout serialization and reconstruction."""
import hashlib
import json
import platform
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from . import __version__
from .config import (
    BoardSpec,
    Config,
    ConfigError,
    Geometry,
    Printing,
    allow_untested_opencv,
    config_from_dict,
    dictionary_for,
)


def _opencv_distribution_version() -> str:
    try:
        return version("opencv-contrib-python-headless")
    except PackageNotFoundError:
        return "unknown"


#: Wheel version of the pinned OpenCV distribution, minus its build suffix. requirements.txt
#: stays the single source of truth instead of duplicating a version literal here.
OPENCV_DISTRIBUTION_VERSION = _opencv_distribution_version()
#: The OpenCV release this generator is verified against, e.g. 4.11.0. When the pinned
#: distribution is not installed (another OpenCV wheel), fall back to the current release.
OPENCV_VERSION = (OPENCV_DISTRIBUTION_VERSION.rsplit(".", 1)[0]
                  if OPENCV_DISTRIBUTION_VERSION != "unknown" else cv2.__version__)


def check_opencv() -> None:
    """Refuse an untested OpenCV release unless the user explicitly accepts it."""
    if cv2.__version__ != OPENCV_VERSION and not allow_untested_opencv():
        raise ConfigError(
            f"Tested OpenCV {OPENCV_VERSION} required; found {cv2.__version__}. Install requirements.txt, or "
            "accept an untested build with --allow-untested-opencv (environment CHARUCO_ALLOW_UNTESTED_OPENCV=1)")
    for name in ("CharucoBoard", "CharucoDetector", "ArucoDetector"):
        if not hasattr(cv2.aruco, name):
            raise ConfigError(f"Missing modern OpenCV API: cv2.aruco.{name}")


def untested_opencv_warning() -> str | None:
    if cv2.__version__ == OPENCV_VERSION:
        return None
    return (f"OpenCV {cv2.__version__} is not the tested {OPENCV_VERSION}: board layout and detector "
            "behaviour were accepted by the user without verification")


def create_board(geometry: Geometry, spec: BoardSpec) -> Any:
    check_opencv()
    Config(geometry, Printing(), (spec,)).validate()
    ids = np.asarray(spec.marker_ids, dtype=np.int32)
    board = cv2.aruco.CharucoBoard(
        (geometry.squares_x, geometry.squares_y), geometry.square_length_mm / 1000.0,
        geometry.marker_length_mm / 1000.0, dictionary_for(geometry.dictionary), ids)
    board.setLegacyPattern(geometry.legacy_pattern)
    if list(np.asarray(board.getIds()).ravel()) != list(spec.marker_ids):
        raise ConfigError("OpenCV did not retain the explicit marker ID sequence")
    return board


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment() -> dict:
    return {
        "python": platform.python_version(), "opencv": cv2.__version__,
        "opencv_tested_against": OPENCV_VERSION,
        "opencv_distribution": "opencv-contrib-python-headless",
        "opencv_distribution_version": OPENCV_DISTRIBUTION_VERSION,
        "numpy": np.__version__, "pillow": version("Pillow"),
        "reportlab": version("reportlab"), "pypdf": version("pypdf"),
    }


def layout_record(geometry: Geometry, spec: BoardSpec, board) -> dict:
    placements = []
    for index, (marker_id, points) in enumerate(zip(spec.marker_ids, board.getObjPoints(), strict=True)):
        center = np.asarray(points).mean(axis=0) * 1000.0
        placements.append({
            "sequence_index": index, "id": marker_id,
            "square_column": round(float(center[0] / geometry.square_length_mm - 0.5)),
            "square_row": round(float(center[1] / geometry.square_length_mm - 0.5)),
            "corners_m": np.asarray(points).tolist(),
        })
    return {
        "name": spec.name, "identifier": spec.identifier,
        "dictionary": geometry.dictionary, "marker_ids": list(spec.marker_ids),
        "geometry": vars(geometry),
        "square_length_m": geometry.square_length_mm / 1000.0,
        "square_length_mm": geometry.square_length_mm,
        "marker_length_m": geometry.marker_length_mm / 1000.0,
        "marker_length_mm": geometry.marker_length_mm,
        "chessboard_squares": [geometry.squares_x, geometry.squares_y],
        "pattern_dimensions_mm": list(geometry.pattern_mm),
        "pattern_dimensions_m": [v / 1000.0 for v in geometry.pattern_mm],
        "expected_marker_count": geometry.marker_count,
        "expected_charuco_corner_count": geometry.corner_count,
        "marker_placement": placements,
        "charuco_corners": [{"id": i, "point_m": point.tolist()}
                            for i, point in enumerate(board.getChessboardCorners())],
        "coordinate_frame": {
            "units": "metres", "origin": "outer top-left corner of pattern viewed from printed side",
            "x": "right along columns", "y": "down along rows",
            "z": "into the board, x cross y, right-handed",
            "plane": "z=0", "charuco_id_order": "row-major internal intersections, starting at zero",
            "marker_corner_order": "top-left, top-right, bottom-right, bottom-left in unrotated print view",
            "opencv_object_point_storage": "float32; stored corner values include its rounding",
        },
        "layout_compatibility": {
            "opencv_version": cv2.__version__, "legacy_pattern": board.getLegacyPattern(),
            "border_bits": geometry.border_bits,
            "top_left_square": "white_with_marker" if geometry.legacy_pattern and geometry.squares_y % 2 == 0 else "black",
            "constructor": "cv2.aruco.CharucoBoard(size, squareLength_m, markerLength_m, dictionary, ids)",
        },
    }


def checksum_mismatch(manifest: dict) -> ConfigError:
    """Distinguish 'saved by another generator build' from 'this manifest was edited'."""
    saved = manifest.get("generator_version", "unknown")
    if saved != __version__:
        return ConfigError(
            f"Manifest was generated by version {saved}, this build is {__version__} and its configuration "
            f"layout differs; regenerate the artifacts before verifying them")
    return ConfigError("Manifest configuration checksum mismatch; the manifest or its configuration was modified")


def reconstruct_board(manifest: dict, name: str):
    """Return an identical OpenCV board, rejecting inconsistent saved layout data."""
    check_opencv()
    if manifest.get("schema_version") != 1:
        raise ConfigError("Unsupported manifest schema")
    if manifest["environment"]["opencv"] != cv2.__version__:
        raise ConfigError(f"Manifest was generated with OpenCV {manifest['environment']['opencv']}, "
                          f"runtime is {cv2.__version__}; board layout may differ. Regenerate with this environment "
                          "or run the matching build")
    config = config_from_dict(manifest["configuration"])
    if canonical_hash(config.to_dict()) != manifest["configuration_sha256"]:
        raise checksum_mismatch(manifest)
    spec = next((b for b in config.boards if b.name == name), None)
    record = next((b for b in manifest["boards"] if b["name"] == name), None)
    if spec is None or record is None:
        raise ConfigError(f"Unknown board: {name}")
    board = create_board(config.geometry, spec)
    expected = layout_record(config.geometry, spec, board)
    for key, value in expected.items():
        if record.get(key) != value:
            raise ConfigError(f"Manifest layout mismatch for {name}: {key}")
    return board
