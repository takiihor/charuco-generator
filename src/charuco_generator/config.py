"""Strict JSON schema and validation, shared by generation and reconstruction."""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re

import cv2


class ConfigError(ValueError):
    """An invalid or unsupported configuration."""


def _keys(value: dict, expected: set[str], context: str) -> None:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be an object")
    extra = set(value) - expected
    if extra:
        raise ConfigError(f"Unknown {context} keys: {sorted(extra)}")


def _integer(value, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be an integer in [{minimum}, {maximum}]")


def _positive(value, name: str, allow_zero: bool = False) -> None:
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (value < 0 if allow_zero else value <= 0)):
        raise ConfigError(f"{name} must be a finite {'nonnegative' if allow_zero else 'positive'} number")


@dataclass(frozen=True)
class Geometry:
    dictionary: str = "DICT_5X5_100"
    squares_x: int = 5
    squares_y: int = 4
    square_length_mm: float = 20.0
    marker_length_mm: float = 14.0
    legacy_pattern: bool = False
    border_bits: int = 1

    @property
    def marker_count(self) -> int:
        return self.squares_x * self.squares_y // 2

    @property
    def corner_count(self) -> int:
        return (self.squares_x - 1) * (self.squares_y - 1)

    @property
    def pattern_mm(self) -> tuple[float, float]:
        return self.squares_x * self.square_length_mm, self.squares_y * self.square_length_mm

    def validate(self) -> None:
        _integer(self.squares_x, "squares_x", 2, 100)
        _integer(self.squares_y, "squares_y", 2, 100)
        _positive(self.square_length_mm, "square_length_mm")
        _positive(self.marker_length_mm, "marker_length_mm")
        if self.marker_length_mm >= self.square_length_mm:
            raise ConfigError("marker_length_mm must be smaller than square_length_mm")
        if type(self.legacy_pattern) is not bool:
            raise ConfigError("legacy_pattern must be a boolean")
        _integer(self.border_bits, "border_bits", 1, 8)
        dictionary_for(self.dictionary)


def dictionary_for(name: str):
    # Only predefined ArUco dictionaries; do not allow arbitrary cv2 attributes.
    if (not isinstance(name, str) or not name.startswith("DICT_")
            or not hasattr(cv2.aruco, name)):
        raise ConfigError(f"Unknown predefined ArUco dictionary: {name!r}")
    try:
        return cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, name))
    except (cv2.error, TypeError) as exc:
        raise ConfigError(f"Invalid dictionary: {name!r}") from exc


def page_size_mm(value: str) -> tuple[float, float]:
    sizes = {"A4": (210.0, 297.0), "A3": (297.0, 420.0), "LETTER": (215.9, 279.4)}
    if isinstance(value, str) and value.upper() in sizes:
        return sizes[value.upper()]
    if isinstance(value, str):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", value.lower())
        if match:
            result = tuple(float(v) for v in match.groups())
            if all(50 <= v <= 2000 for v in result):
                return result
    raise ConfigError("page_size must be A4, A3, LETTER, or WIDTHxHEIGHT in mm (50..2000)")


@dataclass(frozen=True)
class Printing:
    dpi: int = 600
    page_size: str = "A4"
    white_margin_mm: float = 10.0
    page_margin_mm: float = 10.0

    def validate(self) -> None:
        _integer(self.dpi, "dpi", 72, 2400)
        page_size_mm(self.page_size)
        _positive(self.white_margin_mm, "white_margin_mm")
        _positive(self.page_margin_mm, "page_margin_mm", allow_zero=True)
        if self.white_margin_mm < 3:
            raise ConfigError("white_margin_mm must be at least 3 mm for an external quiet margin")


@dataclass(frozen=True)
class BoardSpec:
    name: str
    identifier: str
    marker_ids: tuple[int, ...]


@dataclass(frozen=True)
class Config:
    geometry: Geometry
    printing: Printing
    boards: tuple[BoardSpec, ...]
    output_dir: str = "output"
    schema_version: int = 1

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError("Only schema_version 1 is supported")
        self.geometry.validate()
        self.printing.validate()
        if not isinstance(self.output_dir, str) or not self.output_dir.strip():
            raise ConfigError("output_dir must be a nonempty path string")
        if not self.boards:
            raise ConfigError("At least one board is required")
        names, identifiers, seen_ids = set(), set(), set()
        capacity = dictionary_for(self.geometry.dictionary).bytesList.shape[0]
        for board in self.boards:
            if (not isinstance(board.name, str)
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", board.name)):
                raise ConfigError("Board names must match [a-z][a-z0-9_]{0,31}")
            if (not isinstance(board.identifier, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,47}", board.identifier)):
                raise ConfigError("Board identifiers must be 1..48 ASCII letters/digits/_.- starting with a letter/digit")
            if board.name in names or board.identifier in identifiers:
                raise ConfigError("Board names and identifiers must be unique")
            names.add(board.name)
            identifiers.add(board.identifier)
            if len(board.marker_ids) != self.geometry.marker_count:
                raise ConfigError(f"{board.name}: expected {self.geometry.marker_count} marker IDs, got {len(board.marker_ids)}")
            for marker_id in board.marker_ids:
                _integer(marker_id, f"{board.name} marker ID", 0, capacity - 1)
                if marker_id in seen_ids:
                    raise ConfigError(f"Duplicate/overlapping marker ID: {marker_id}")
                seen_ids.add(marker_id)

    def to_dict(self) -> dict:
        # JSON round-trip also normalizes tuples for comparisons and JSON serialization.
        return json.loads(json.dumps(asdict(self)))


def config_from_dict(data: dict) -> Config:
    _keys(data, {"schema_version", "geometry", "printing", "boards", "output_dir"}, "configuration")
    try:
        geometry = data.get("geometry", {})
        printing = data.get("printing", {})
        _keys(geometry, set(Geometry.__dataclass_fields__), "geometry")
        _keys(printing, set(Printing.__dataclass_fields__), "printing")
        boards = []
        for item in data["boards"]:
            _keys(item, {"name", "identifier", "marker_ids"}, "board")
            if not isinstance(item["marker_ids"], list):
                raise ConfigError("marker_ids must be an explicit JSON array")
            boards.append(BoardSpec(item["name"], item["identifier"], tuple(item["marker_ids"])))
        config = Config(Geometry(**geometry), Printing(**printing), tuple(boards),
                        data.get("output_dir", "output"), data.get("schema_version", 1))
    except (KeyError, TypeError) as exc:
        raise ConfigError(f"Malformed configuration: {exc}") from exc
    config.validate()
    return config


def load_config(path: str | Path) -> Config:
    try:
        return config_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in {path}: {exc}") from exc
