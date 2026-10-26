"""Board selection and consecutive marker ID allocation for CLI and menu use."""
import hashlib
import json
from pathlib import Path

from .config import Config, ConfigError, Geometry, config_from_dict, dictionary_for


def select_boards(data: dict, names: list[str] | None) -> None:
    if names is None:
        return
    known = {board["name"] for board in data["boards"]}
    if not names or len(set(names)) != len(names) or set(names) - known:
        raise ConfigError(f"Select distinct board names from: {', '.join(sorted(known))}")
    data["boards"] = [board for board in data["boards"] if board["name"] in names]


def assign_ids(data: dict, start: int) -> None:
    geometry = Geometry(**data["geometry"])
    geometry.validate()
    capacity = dictionary_for(geometry.dictionary).bytesList.shape[0]
    count = geometry.marker_count * len(data["boards"])
    if type(start) is not int or start < 0 or start + count > capacity:
        raise ConfigError(f"Need {count} consecutive IDs; first ID must be 0..{capacity - count} "
                          f"for {geometry.dictionary} ({capacity} IDs)")
    for index, board in enumerate(data["boards"]):
        first = start + index * geometry.marker_count
        board["marker_ids"] = list(range(first, first + geometry.marker_count))
        # A new ID set gets a distinct, deterministic physical-reference identifier.
        identity = json.dumps([data["geometry"], board["marker_ids"]], sort_keys=True)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        board["identifier"] = f"{board['name']}-ref-{digest}"


def next_ids(config: Config, data: dict, root: Path) -> int:
    """Find an unused block within the config and saved output tree (same dictionary)."""
    geometry = Geometry(**data["geometry"])
    geometry.validate()
    capacity = dictionary_for(geometry.dictionary).bytesList.shape[0]
    used = set()
    if config.geometry.dictionary == geometry.dictionary:
        used.update(marker for board in config.boards for marker in board.marker_ids)
    for path in sorted(root.rglob("board_manifest.json")):
        try:
            saved = config_from_dict(json.loads(path.read_text(encoding="utf-8"))["configuration"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"Cannot check saved IDs in {path}: {exc}") from exc
        if saved.geometry.dictionary == geometry.dictionary:
            used.update(marker for board in saved.boards for marker in board.marker_ids)
    count = geometry.marker_count * len(data["boards"])
    run = 0
    for marker in range(capacity):
        run = 0 if marker in used else run + 1
        if run == count:
            return marker - count + 1
    raise ConfigError(f"No unused block of {count} IDs in {geometry.dictionary}. "
                      "Choose fewer boards or a larger dictionary, or explicitly reuse an existing ID set.")
