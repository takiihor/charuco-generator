"""Command line entry points for generation and synthetic/photographic validation."""
import argparse
import json
from pathlib import Path
import re
import sys

import cv2

from .board import create_board, layout_record, reconstruct_board, sha256
from .config import ConfigError, config_from_dict, load_config
from .generator import generate, write_json
from .render import raster_metrics
from .selection import assign_ids, select_boards
from .validation import ValidationError, load_manifest, validate_artifacts, validate_image


def _parse_ids(value: str) -> list[int]:
    result = []
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
    for key in ("dpi", "page_size", "white_margin_mm", "page_margin_mm"):
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


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Actual-size ChArUco board generator and detector")
    commands = root.add_subparsers(dest="command", required=True)
    menu = commands.add_parser("menu", help="Choose boards, settings and IDs in a guided terminal menu")
    menu.add_argument("--config", default="config/boards.json")
    menu.add_argument("--output-dir", help="Root folder for saved board sets and unused-ID lookup")
    menu.add_argument("--advanced", action="store_true", help="Also choose grid, marker size, dictionary, paper, IDs and save folder")
    gen = commands.add_parser("generate", help="Generate all configured boards and verify saved artifacts")
    gen.add_argument("--config", default="config/boards.json")
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
    gen.add_argument("--output-dir")
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
    val.add_argument("--report", help="Also save JSON report to this path")
    verify = commands.add_parser("verify", help="Recheck every generated artifact, PDF geometry and checksum")
    verify.add_argument("--manifest", default="output/board_manifest.json")
    verify.add_argument("--report")
    return root


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["menu"] if sys.stdin.isatty() else ["generate"]
    args = parser().parse_args(argv)
    try:
        if args.command in ("generate", "menu"):
            if args.command == "menu":
                from .menu import configure
                config = configure(args.config, args.output_dir, advanced=args.advanced)
                if config is None:
                    return 0
            else:
                config = _overrides(load_config(args.config), args)
            report = generate(config)
            print(f"Generated {len(config.boards)} verified boards in {config.output_dir}")
            for board in report["boards"]:
                print(f"  {board['board']}: {board['detected_marker_count']} markers, {board['detected_charuco_corner_count']} ChArUco corners")
            print("PDF geometry verified. Printed dimensions still require physical measurement.")
            print(f"Print files: {Path(config.output_dir).resolve()} (PDFs at Actual size / 100%)")
            return 0
        if args.command == "verify":
            path = Path(args.manifest)
            manifest = load_manifest(path)
            report = validate_artifacts(path.parent, manifest)
            report["manifest_sha256"] = sha256(path)
        else:
            manifest_path = Path(args.manifest or "output/board_manifest.json")
            if args.manifest or (not args.config and manifest_path.is_file()):
                manifest = load_manifest(manifest_path)
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
            report = validate_image(args.input, record, board, synthetic=synthetic,
                                    require_complete=args.require_complete)
        if args.report:
            write_json(Path(args.report), report)
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
        return 0 if report["passed"] else 1
    except ValidationError as exc:
        print(json.dumps(exc.report, indent=2, sort_keys=True, allow_nan=False), file=sys.stderr)
        return 1
    except (ConfigError, OSError, ValueError, KeyError, cv2.error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
