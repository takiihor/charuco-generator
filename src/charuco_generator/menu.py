"""Simple terminal setup, with technical choices available in advanced mode."""
from fractions import Fraction
import math
from pathlib import Path
import re

from .config import ConfigError, Geometry, config_from_dict, dictionary_for, load_config, page_size_mm
from .render import plan_pages, raster_metrics
from .selection import assign_ids, next_ids, select_boards


def _ask(label, default, parse=str):
    while True:
        value = input(f"{label} [{default}]: ").strip()
        try:
            return parse(value if value else str(default))
        except (ConfigError, ValueError) as exc:
            print(f"  {exc}")


def _choice(label, default, choices):
    def parse(value):
        if value.lower() not in choices:
            raise ValueError(f"Choose {', '.join(choices)}.")
        return value.lower()
    return _ask(label, default, parse)


def _customize(data):
    geometry = data["geometry"]
    printing = data["printing"]

    def grid(value):
        match = re.fullmatch(r"(\d+)\s*[x×]\s*(\d+)", value.lower())
        if not match or not all(2 <= int(v) <= 100 for v in match.groups()):
            raise ValueError("Enter columns x rows, e.g. 3x3 (2..100 each).")
        return tuple(int(v) for v in match.groups())

    geometry["squares_x"], geometry["squares_y"] = _ask(
        "Grid of squares (columns x rows)", f"{geometry['squares_x']}x{geometry['squares_y']}", grid)

    def length(value):
        try:
            result = float(value)
        except ValueError:
            raise ValueError("Enter a positive size in mm, e.g. 20.") from None
        if not math.isfinite(result) or result <= 0:
            raise ValueError("Enter a positive size in mm, e.g. 20.")
        return result

    geometry["square_length_mm"] = _ask("Square size in mm", geometry["square_length_mm"], length)

    def marker(value):
        result = length(value)
        if result >= geometry["square_length_mm"]:
            raise ValueError(f"Marker size must be smaller than the {geometry['square_length_mm']:g} mm square.")
        return result

    geometry["marker_length_mm"] = _ask("Marker size in mm", round(geometry["square_length_mm"] * 0.7, 8), marker)

    def dictionary(value):
        dictionary_for(value)
        return value

    def paper(value):
        page_size_mm(value)
        return value.upper()

    def dpi(value):
        if not value.isdigit() or not 72 <= int(value) <= 2400:
            raise ValueError("Enter a whole number from 72 to 2400.")
        return int(value)

    geometry["dictionary"] = _ask("ArUco dictionary", geometry["dictionary"], dictionary)
    printing["page_size"] = _ask("Paper (A4, A3, LETTER or WIDTHxHEIGHT mm)", printing["page_size"], paper)
    printing["dpi"] = _ask("PNG resolution in DPI", printing["dpi"], dpi)


def _pattern_size(value, data):
    """Convert overall pattern dimensions to a grid of equal-size squares."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*mm)?", value.lower())
    if not match or len(value) > 80:
        raise ConfigError("Enter width x height in mm, e.g. 30x30 or 100x80.")
    width, height = (Fraction(v) for v in match.groups())
    if width <= 0 or height <= 0:
        raise ConfigError("Width and height must be greater than zero.")
    geometry = data["geometry"].copy()
    ratio = width / height
    if ratio != Fraction(geometry["squares_x"], geometry["squares_y"]):
        columns, rows = ratio.numerator, ratio.denominator
        # Keep at least four internal corners, including for square patterns.
        factor = max(math.ceil(Fraction(3, columns)), math.ceil(Fraction(3, rows)))
        geometry["squares_x"], geometry["squares_y"] = columns * factor, rows * factor
    if max(geometry["squares_x"], geometry["squares_y"]) > 100:
        raise ConfigError("Use a simpler width-to-height ratio, e.g. 30x30, 100x80 or 120x80; "
                          "custom grids are available with ./generate_charuco.sh menu --advanced.")
    geometry["square_length_mm"] = float(width / geometry["squares_x"])
    geometry["marker_length_mm"] = geometry["square_length_mm"] * 0.7
    probe = {**data, "geometry": geometry, "boards": [b.copy() for b in data["boards"]]}
    assign_ids(probe, 0)
    checked = config_from_dict(probe)
    plan_pages(checked, checked.boards)
    raster_metrics(checked.geometry, checked.printing)
    return geometry


def _new_directory(root, boards):
    label = "_".join(f"{b['name']}_{min(b['marker_ids'])}-{max(b['marker_ids'])}" for b in boards)
    path = root / label
    suffix = 2
    while path.exists():
        path = root / f"{label}_{suffix}"
        suffix += 1
    return path


def configure(config_path: str, output_root: str | None = None, *, advanced: bool = False):
    """Return a reviewed config, or None when cancelled; never edit the source config."""
    config = load_config(config_path)
    data = config.to_dict()
    root = Path(output_root or config.output_dir)
    print("\nChArUco board generator\nPress Enter to use the value in brackets. Ctrl+C cancels.\n")
    print("Available boards:")
    names = [board.name for board in config.boards]
    for index, name in enumerate(names, 1):
        print(f"  {index}. {name}")
    print("Choose one target, or all for two targets with different IDs." if len(names) == 2
          else "Choose the targets to generate.")

    def selection(value):
        if value.lower() == "all":
            return names
        chosen = []
        for token in value.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(names):
                token = names[int(token) - 1]
            chosen.append(token)
        probe = config.to_dict()
        select_boards(probe, chosen)
        return chosen

    select_boards(data, _ask("Boards (numbers or names, separated by commas; all)", "all", selection))
    g = config.geometry
    if advanced:
        _customize(data)
    else:
        width, height = g.pattern_mm
        print("\nEnter the overall black-and-white pattern size, excluding the white margin.")
        data["geometry"] = _ask("Pattern size in mm (width x height)", f"{width:g}x{height:g}",
                                lambda value: _pattern_size(value, data))

    id_default = "1" if advanced else "2"
    while True:
        print("\nMarker IDs: 1 = new unused IDs, 2 = keep configured IDs, 3 = choose first ID")
        mode = _choice("ID option", id_default, ("1", "2", "3"))
        try:
            if mode == "1":
                print(f"Checking configured IDs and saved manifests under {root}...")
                assign_ids(data, next_ids(config, data, root))
            elif mode == "3":
                assign_ids(data, _ask("First marker ID (remaining IDs assigned automatically)", "0", int))
            else:
                originals = {board.name: list(board.marker_ids) for board in config.boards}
                for board in data["boards"]:
                    board["marker_ids"] = originals[board["name"]]
                    board["identifier"] = next(b.identifier for b in config.boards if b.name == board["name"])
            checked = config_from_dict(data)
            plan_pages(checked, checked.boards)
            raster_metrics(checked.geometry, checked.printing)
            break
        except ConfigError as exc:
            if not advanced:
                print(f"  {exc}")
                print("  Choose another ID option (3 = choose first ID), or run ./generate_charuco.sh menu --advanced.")
                continue
            print(f"  {exc}")
            if _choice("Settings: 1 = keep, 2 = customize", "1", ("1", "2")) == "2":
                _customize(data)

    default_path = _new_directory(root, data["boards"])
    data["output_dir"] = _ask("Save folder", str(default_path)) if advanced else str(default_path)
    checked = config_from_dict(data)
    print("\nReady to generate:")
    for board in checked.boards:
        print(f"  {board.name}: IDs {', '.join(map(str, board.marker_ids))}")
    width, height = checked.geometry.pattern_mm
    print(f"  Pattern: {width:g} x {height:g} mm; paper: {checked.printing.page_size}")
    g = checked.geometry
    print(f"  Grid: {g.squares_x} x {g.squares_y}; squares: {g.square_length_mm:g} mm; markers: {g.marker_length_mm:g} mm")
    print(f"  Save to: {Path(checked.output_dir).resolve()}")
    print("  Existing files in this folder will be replaced." if Path(checked.output_dir).exists()
          else "  A new folder will be created.")
    if _choice("Generate now? y/n", "y", ("y", "n")) == "n":
        print("Cancelled. No files generated.")
        return None
    return checked
