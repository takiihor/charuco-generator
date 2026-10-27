"""Command line entry points for generation and synthetic/photographic validation."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import cv2

from . import __version__
from .board import create_board, layout_record, reconstruct_board, sha256
from .config import ALLOW_UNTESTED_OPENCV_ENV, ConfigError, Printing, config_from_dict, load_config
from .generator import generate, write_json
from .render import raster_metrics
from .selection import assign_ids, select_boards
from .validation import (
    DETECTOR_OVERRIDES,
    ValidationError,
    load_manifest,
    manifest_kind,
    validate_artifacts,
    validate_image,
)


def _parse_ids(value: str) -> list[int]:
    result: list[int] = []
    for token in value.split(","):
        if re.fullmatch(r"\d+-\d+", token):
            first, last = map(int, token.split("-"))
            if last < first or last - first > 10000:
                raise ConfigError(f"Invalid marker ID range: {token}")
            result.extend(range(first, last + 1))
        elif re.fullmatch(r"\d+", token):
            result.append(int(token))
        else:
            raise ConfigError("IDs must be comma-separated integers or inclusive ranges, e.g. 10-19 or 9,2,7")
    return result


def _overrides(config, args):
    data = config.to_dict()
    for key in ("squares_x", "squares_y", "square_length_mm", "marker_length_mm", "dictionary", "legacy_pattern", "border_bits"):
        value = getattr(args, key)
        if value is not None:
            data["geometry"][key] = value
    for key in ("dpi", "page_size", "white_margin_mm", "page_margin_mm", "tile_overlap_mm"):
        value = getattr(args, key)
        if value is not None:
            data["printing"][key] = value
    if args.output_dir is not None:
        data["output_dir"] = args.output_dir
    select_boards(data, args.select)
    if args.board and args.board not in [b["name"] for b in data["boards"]]:
        raise ConfigError(f"Unknown board: {args.board}")
    overridden = set()
    for override in args.marker_ids or []:
        if ":" in override:
            name, value = override.split(":", 1)
        elif args.board:
            name, value = args.board, override
        elif len(data["boards"]) == 1:
            name, value = data["boards"][0]["name"], override
        else:
            raise ConfigError("Use --marker-ids front:0-9 or --board front with unqualified IDs")
        if name in overridden:
            raise ConfigError(f"Repeated marker ID override for {name}")
        overridden.add(name)
        spec = next((b for b in data["boards"] if b["name"] == name), None)
        if spec is None:
            raise ConfigError(f"Unknown board for ID override: {name}")
        spec["marker_ids"] = _parse_ids(value)
    if args.start_id is not None:
        assign_ids(data, args.start_id)
    return config_from_dict(data)


def _add_output_flags(sub: argparse.ArgumentParser, *, dictionary_export: bool) -> None:
    """Flags that control what lands in the output folder, shared by every generating command."""
    sub.add_argument("--clean", action="store_true",
                    help="Delete artifact files already in the output folder that this configuration does not produce "
                         "(default: refuse to generate over them)")
    sub.add_argument("--export-svg", action="store_true", help="Also write SVG vector copies into the vector/ subfolder")
    sub.add_argument("--export-dxf", action="store_true", help="Also write DXF (R12) vector copies into the vector/ subfolder")
    if dictionary_export:
        sub.add_argument("--export-dictionary-yml", action="store_true",
                         help="Also write the OpenCV dictionary YAML into the vector/ subfolder")
    sub.add_argument("--allow-untested-opencv", action="store_true",
                    help=f"Run against an OpenCV build other than the tested one; every report then carries a warning "
                         f"(sets {ALLOW_UNTESTED_OPENCV_ENV}=1)")


def _detector_overrides(args) -> dict:
    values = {name: getattr(args, name) for name in DETECTOR_OVERRIDES}
    values["corner_refinement"] = args.corner_refinement
    return {name: value for name, value in values.items() if value is not None}


def _id_label(marker_ids: list[int]) -> str:
    if marker_ids == list(range(marker_ids[0], marker_ids[-1] + 1)):
        return str(marker_ids[0]) if len(marker_ids) == 1 else f"{marker_ids[0]}-{marker_ids[-1]}"
    shown = ", ".join(map(str, marker_ids[:12]))
    return shown + (" ..." if len(marker_ids) > 12 else "")


def _print_quality(report: dict) -> None:
    if (quality := report.get("dictionary_quality")):
        print(f"  Dictionary: capacity {quality['capacity']}, {quality['marker_size']}x{quality['marker_size']} modules, "
              f"min Hamming distance {quality['min_hamming_distance']}, "
              f"distance within the IDs used: {quality['min_distance_in_use']}")
    for warning in report.get("warnings", []):
        print(f"  WARNING: {warning}", file=sys.stderr)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Actual-size ChArUco board and ArUco marker generator")
    root.add_argument("--version", action="version", version=f"charuco-generator {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    menu = commands.add_parser("menu", help="Choose boards, settings and IDs in a guided terminal menu")
    menu.add_argument("--config", default="config/boards.json")
    menu.add_argument("--output-dir", help="Root folder for saved board sets and unused-ID lookup")
    menu.add_argument("--advanced", action="store_true", help="Also choose grid, marker size, dictionary, paper, IDs and save folder")
    menu.add_argument("--ignore-unreadable-manifests", action="store_true",
                      help="Allocate unused IDs even when a saved manifest under --output-dir cannot be read")
    _add_output_flags(menu, dictionary_export=True)
    gen = commands.add_parser("generate", help="Generate all configured boards and verify saved artifacts")
    source = gen.add_mutually_exclusive_group()
    source.add_argument("--config", default="config/boards.json")
    source.add_argument("--from-manifest",
                        help="Reproduce the exact configuration saved in a board manifest, writing into that "
                             "manifest's own folder unless --output-dir is given")
    gen.add_argument("--select", nargs="+", metavar="NAME", help="Generate only these boards (default: all)")
    gen.add_argument("--board", help="Target board for unqualified ID overrides; all boards are still generated")
    gen.add_argument("--squares-x", type=int)
    gen.add_argument("--squares-y", type=int)
    gen.add_argument("--square-size-mm", "--square-length-mm", dest="square_length_mm", type=float)
    gen.add_argument("--marker-size-mm", "--marker-length-mm", dest="marker_length_mm", type=float)
    gen.add_argument("--dictionary")
    ids = gen.add_mutually_exclusive_group()
    ids.add_argument("--marker-ids", action="append", help="Repeatable NAME:ID,ID or NAME:FIRST-LAST (inclusive)")
    ids.add_argument("--start-id", type=int, help="Assign consecutive, nonoverlapping IDs to selected boards starting here")
    gen.add_argument("--legacy-pattern", action=argparse.BooleanOptionalAction, default=None)
    gen.add_argument("--border-bits", type=int)
    gen.add_argument("--dpi", type=int)
    gen.add_argument("--page-size", help="A4, A3, LETTER or WIDTHxHEIGHT in mm")
    gen.add_argument("--white-margin-mm", type=float)
    gen.add_argument("--page-margin-mm", type=float)
    gen.add_argument("--tile-overlap-mm", type=float,
                    help="Split a board that cannot fit one page across tiled sheets, overlapping by this many mm "
                         "(default 0: refuse and report). Assembly seams add error; a single rigid sheet is preferred")
    gen.add_argument("--output-dir")
    _add_output_flags(gen, dictionary_export=True)
    aruco = commands.add_parser("aruco", help="Generate standalone ArUco markers")
    aruco.add_argument("--ids", default="1-20", help="Marker IDs: comma-separated integers or inclusive ranges (default: 1-20)")
    aruco.add_argument("--marker-size-mm", "--marker-length-mm", dest="marker_length_mm", type=float, default=30.0,
                       help="Marker side including its black border, excluding white margin (default: 30 mm)")
    aruco.add_argument("--dictionary", default="DICT_5X5_100")
    aruco.add_argument("--border-bits", type=int, default=1)
    aruco.add_argument("--dpi", type=int, default=600)
    aruco.add_argument("--page-size", default="A4", help="A4, A3, LETTER or WIDTHxHEIGHT in mm")
    aruco.add_argument("--white-margin-mm", type=float, default=10.0)
    aruco.add_argument("--page-margin-mm", type=float, default=10.0)
    aruco.add_argument("--output-dir", default="output/aruco")
    _add_output_flags(aruco, dictionary_export=True)
    val = commands.add_parser("validate", help="Report marker/corner detection in a PNG or printed-board photograph")
    val.add_argument("--input", required=True)
    val.add_argument("--board", required=True)
    sources = val.add_mutually_exclusive_group()
    sources.add_argument("--manifest", help="Use saved geometry; default output/board_manifest.json if available")
    sources.add_argument("--config", help="Use a config instead of a manifest")
    mode = val.add_mutually_exclusive_group()
    mode.add_argument("--synthetic", action="store_true", help="Require complete detection, exact PNG geometry and clear margins")
    mode.add_argument("--photo", action="store_true", help="Force photograph mode; allow partial visibility")
    val.add_argument("--require-complete", action="store_true", help="Require all markers/corners in a photograph")
    val.add_argument("--corner-refinement", choices=("none", "subpix", "contour"), default="none",
                    help="Sub-pixel corner method for photographs; 'none' keeps synthetic geometry checks exact")
    for name, (attribute, low, high) in DETECTOR_OVERRIDES.items():
        val.add_argument(f"--{name.replace('_', '-')}", dest=name, type=float,
                         help=f"Tune cv2.aruco detector {attribute} (allowed {low}..{high}); only for photographs that miss markers")
    val.add_argument("--report", help="Also save JSON report to this path")
    val.add_argument("--allow-untested-opencv", action="store_true",
                     help=f"Run against an OpenCV build other than the tested one (sets {ALLOW_UNTESTED_OPENCV_ENV}=1)")
    verify = commands.add_parser("verify", help="Recheck ChArUco or ArUco artifacts, PDF geometry and checksums")
    verify.add_argument("--manifest", default="output/board_manifest.json")
    verify.add_argument("--report")
    verify.add_argument("--allow-untested-opencv", action="store_true",
                        help=f"Run against an OpenCV build other than the tested one (sets {ALLOW_UNTESTED_OPENCV_ENV}=1)")
    return root


def _aruco_branch(args) -> int:
    from .aruco import ArucoConfig, generate_aruco
    config = ArucoConfig(args.dictionary, args.marker_length_mm, args.border_bits,
                         Printing(args.dpi, args.page_size, args.white_margin_mm, args.page_margin_mm),
                         tuple(_parse_ids(args.ids)))
    report = generate_aruco(config, args.output_dir, clean=args.clean,
                            svg=args.export_svg, dxf=args.export_dxf, dictionary_file=args.export_dictionary_yml)
    marker_ids = sorted(config.marker_ids)
    print(f"Generated {len(marker_ids)} verified ArUco markers (IDs {_id_label(marker_ids)}) in {Path(args.output_dir).resolve()}")
    print(f"Dictionary: {config.dictionary}; marker size: {config.marker_length_mm:g} x {config.marker_length_mm:g} mm")
    _print_quality(report)
    print("Print the PDFs at Actual size / 100% and measure both scale bars on each panel.")
    return 0


def _base_config(args):
    """Configuration to override from: a config file or a previously saved board manifest."""
    if not getattr(args, "from_manifest", None):
        return load_config(args.config)
    path = Path(args.from_manifest)
    manifest = load_manifest(path)
    if manifest_kind(manifest) == "aruco":
        raise ConfigError(f"{path} describes standalone ArUco markers, which have no board configuration. "
                          "Reproduce them with 'aruco --ids ...' instead")
    data = dict(manifest["configuration"])
    # The saved output_dir is only meaningful on the machine and folder that produced it; the manifest's
    # own directory always is.
    data["output_dir"] = str(path.parent)
    return config_from_dict(data)


def _generate_branch(args) -> int:
    if args.command == "menu":
        from .menu import configure
        config = configure(args.config, args.output_dir, advanced=args.advanced,
                           ignore_unreadable_manifests=args.ignore_unreadable_manifests)
        if config is None:
            return 0
    else:
        config = _overrides(_base_config(args), args)
    report = generate(config, clean=args.clean, svg=args.export_svg, dxf=args.export_dxf,
                      dictionary_file=args.export_dictionary_yml)
    print(f"Generated {len(config.boards)} verified boards in {config.output_dir}")
    for board in report["boards"]:
        print(f"  {board['board']}: {board['detected_marker_count']} markers, {board['detected_charuco_corner_count']} ChArUco corners")
    _print_quality(report)
    print("PDF geometry verified. Printed dimensions still require physical measurement.")
    print(f"Print files: {Path(config.output_dir).resolve()} (PDFs at Actual size / 100%)")
    return 0


def _validate_branch(args) -> dict:
    manifest_path = Path(args.manifest or "output/board_manifest.json")
    use_manifest = bool(args.manifest) or (not args.config and manifest_path.is_file())
    if use_manifest:
        manifest = load_manifest(manifest_path)
        if manifest_kind(manifest) == "aruco":
            raise ConfigError(
                f"{manifest_path} describes standalone ArUco markers, which have no board geometry to detect. "
                f"Recheck them with 'verify --manifest {manifest_path}', or pass a ChArUco board manifest with --manifest")
        board = reconstruct_board(manifest, args.board)
        record = next(b for b in manifest["boards"] if b["name"] == args.board)
        known_png = manifest["sha256"][record["files"]["png"]]
        exact_generated = Path(args.input).is_file() and sha256(args.input) == known_png
        synthetic = args.synthetic or (exact_generated and not args.photo)
    else:
        config = load_config(args.config or "config/boards.json")
        spec = next((b for b in config.boards if b.name == args.board), None)
        if spec is None:
            raise ConfigError(f"Unknown board: {args.board}")
        board = create_board(config.geometry, spec)
        record = layout_record(config.geometry, spec, board)
        record["raster"] = raster_metrics(config.geometry, config.printing)
        synthetic = args.synthetic
    return validate_image(args.input, record, board, synthetic=synthetic, require_complete=args.require_complete,
                          detector_overrides=_detector_overrides(args))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["menu"] if sys.stdin.isatty() else ["generate"]
    args = parser().parse_args(argv)
    if getattr(args, "allow_untested_opencv", False):
        os.environ[ALLOW_UNTESTED_OPENCV_ENV] = "1"
    try:
        if args.command == "aruco":
            return _aruco_branch(args)
        if args.command in ("generate", "menu"):
            return _generate_branch(args)
        if args.command == "verify":
            path = Path(args.manifest)
            manifest = load_manifest(path)
            if manifest_kind(manifest) == "aruco":
                from .aruco import validate_aruco_artifacts
                report = validate_aruco_artifacts(path.parent, manifest)
            else:
                report = validate_artifacts(path.parent, manifest)
            report["manifest_sha256"] = sha256(path)
        else:
            report = _validate_branch(args)
        if args.report:
            write_json(Path(args.report), report)
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["passed"] else 1
    except ValidationError as exc:
        print(json.dumps(exc.report, indent=2, sort_keys=True, allow_nan=False), file=sys.stderr)
        return 1
    # KeyError stays out of this list: a missing key here is a bug in this program or an unchecked
    # manifest, and manifests are schema-checked by load_manifest before anything reads their keys.
    except (ConfigError, OSError, ValueError, cv2.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
