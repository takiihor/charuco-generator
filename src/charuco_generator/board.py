"""Modern OpenCV board construction, layout serialization and reconstruction."""
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform

import cv2
import numpy as np

from .config import BoardSpec, Config, ConfigError, Geometry, Printing, config_from_dict, dictionary_for

OPENCV_VERSION = "4.11.0"


def check_opencv() -> None:
    if cv2.__version__ != OPENCV_VERSION:
        raise ConfigError(f"Tested OpenCV {OPENCV_VERSION} required; found {cv2.__version__}. Install requirements.txt")
    for name in ("CharucoBoard", "CharucoDetector", "ArucoDetector"):
        if not hasattr(cv2.aruco, name):
            raise ConfigError(f"Missing modern OpenCV API: cv2.aruco.{name}")


def create_board(geometry: Geometry, spec: BoardSpec):
    check_opencv()
    Config(geometry, Printing(), (spec,)).validate()
    ids = np.asarray(spec.marker_ids, dtype=np.int32)
    board = cv2.aruco.CharucoBoard(
        (geometry.squares_x, geometry.squares_y), geometry.square_length_mm / 1000.0,
        geometry.marker_length_mm / 1000.0, dictionary_for(geometry.dictionary), ids)
    board.setLegacyPattern(geometry.legacy_pattern)
    if board.getIds().ravel().tolist() != list(spec.marker_ids):
        raise ConfigError("OpenCV did not retain the explicit marker ID sequence")
    return board


def sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def environment() -> dict:
    return {
        "python": platform.python_version(), "opencv": cv2.__version__,
        "opencv_distribution": "opencv-contrib-python-headless",
        "opencv_distribution_version": version("opencv-contrib-python-headless"),
        "numpy": np.__version__, "pillow": version("Pillow"),
        "reportlab": version("reportlab"), "pypdf": version("pypdf"),
    }


def layout_record(geometry: Geometry, spec: BoardSpec, board) -> dict:
    placements = []
    for index, (marker_id, points) in enumerate(zip(spec.marker_ids, board.getObjPoints(), strict=True)):
        center = np.asarray(points).mean(axis=0) * 1000.0
        placements.append({
            "sequence_index": index, "id": marker_id,
            "square_column": int(round(float(center[0] / geometry.square_length_mm - 0.5))),
            "square_row": int(round(float(center[1] / geometry.square_length_mm - 0.5))),
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


def reconstruct_board(manifest: dict, name: str):
    """Return an identical OpenCV board, rejecting inconsistent saved layout data."""
    check_opencv()
    if manifest.get("schema_version") != 1:
        raise ConfigError("Unsupported manifest schema")
    if manifest["environment"]["opencv"] != cv2.__version__:
        raise ConfigError("Manifest OpenCV version differs from runtime")
    config = config_from_dict(manifest["configuration"])
    if canonical_hash(config.to_dict()) != manifest["configuration_sha256"]:
        raise ConfigError("Manifest configuration checksum mismatch")
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
