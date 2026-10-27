"""Board selection and consecutive marker ID allocation for CLI and menu use."""
import hashlib
import itertools
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
    if count > capacity:
        raise ConfigError(f"Need {count} distinct IDs but {geometry.dictionary} holds only {capacity}; "
                          "use fewer or larger squares, fewer boards, or a larger dictionary")
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


def ids_label(marker_ids) -> str:
    """Filesystem-safe summary of an ID set; only genuinely consecutive runs collapse to a range."""
    ids = sorted(set(marker_ids))
    if not ids:
        return "no-ids"
    runs, start = [], ids[0]
    for previous, current in itertools.pairwise(ids):
        if current != previous + 1:
            runs.append((start, previous))
            start = current
    runs.append((start, ids[-1]))
    return "+".join(str(first) if first == last else f"{first}-{last}" for first, last in runs)


#: Both artifact kinds record the marker IDs they consumed, so both reserve them.
MANIFEST_NAMES = ("board_manifest.json", "aruco_manifest.json")


def _manifest_ids(path: Path) -> tuple[str, list[int]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ConfigError("Saved manifest must contain a JSON object")
    if document.get("kind") == "aruco":
        return document["configuration"]["dictionary"], list(document["marker_ids"])
    saved = config_from_dict(document["configuration"])
    return saved.geometry.dictionary, [marker for board in saved.boards for marker in board.marker_ids]


def next_ids(config: Config, data: dict, root: Path, *, ignore_unreadable: bool = False,
             warnings: list[str] | None = None) -> int:
    """Find an unused block within the config and saved output tree (same dictionary)."""
    geometry = Geometry(**data["geometry"])
    geometry.validate()
    capacity = dictionary_for(geometry.dictionary).bytesList.shape[0]
    used: set[int] = set()
    if config.geometry.dictionary == geometry.dictionary:
        used.update(marker for board in config.boards for marker in board.marker_ids)
    unreadable = []
    for path in sorted(candidate for name in MANIFEST_NAMES for candidate in root.rglob(name)):
        try:
            dictionary, marker_ids = _manifest_ids(path)
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            unreadable.append((path, exc))
            continue
        if dictionary == geometry.dictionary:
            used.update(marker_ids)
    if unreadable:
        details = "\n  ".join(f"{path}: {exc}" for path, exc in unreadable)
        if not ignore_unreadable:
            raise ConfigError("Saved manifests could not be read, so their IDs cannot be excluded:\n  " + details
                              + "\nRepair or delete them, or rerun with --ignore-unreadable-manifests to allocate "
                                "IDs without checking those files")
        if warnings is not None:
            warnings.append("Ignored unreadable manifest(s) when choosing marker IDs: "
                            + ", ".join(str(path) for path, _ in unreadable))
    count = geometry.marker_count * len(data["boards"])
    run = 0
    for marker in range(capacity):
        run = 0 if marker in used else run + 1
        if run == count:
            return marker - count + 1
    raise ConfigError(f"No unused block of {count} IDs in {geometry.dictionary}. "
                      "Choose fewer boards or a larger dictionary, or explicitly reuse an existing ID set.")
