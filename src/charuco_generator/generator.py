"""Generate into staging, validate saved files, then publish successful artifacts."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from . import __version__
from .board import canonical_hash, create_board, environment, layout_record, sha256
from .config import Config
from .render import plan_pages, raster_metrics, render_pdf, render_png
from .validation import ValidationError, load_manifest, validate_artifacts


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def generate(config: Config, output_dir: str | Path | None = None) -> dict:
    config.validate()
    # Fail before rendering or publishing when dimensions/resources cannot be supported.
    plan_pages(config, config.boards)
    raster_metrics(config.geometry, config.printing)
    target = Path(config.output_dir if output_dir is None else output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".charuco-staging-", dir=target.parent) as staging:
        directory = Path(staging)
        manifest = {
            "schema_version": 1, "generator_version": __version__, "environment": environment(),
            "configuration": config.to_dict(), "configuration_sha256": canonical_hash(config.to_dict()),
            "path_convention": "All file paths are relative to the directory containing this manifest",
            "print_geometry": "Vector forms in PDF points (72 points/inch); no fit-to-page transformation",
            "printing": {"page_size": config.printing.page_size, "scale": 1.0,
                         "viewer_print_scaling": "None", "physical_print_verified": False},
            "boards": [], "pdf_layouts": {}, "sha256": {},
            "validation_report": "validation_report.json",
            "checksum_scope": "PNG and PDF artifacts. Manifest and report are excluded to avoid recursive checksums; report includes manifest checksum.",
        }
        boards = {spec.name: create_board(config.geometry, spec) for spec in config.boards}
        for spec in config.boards:
            record = layout_record(config.geometry, spec, boards[spec.name])
            png_name, pdf_name = f"{spec.name}_charuco.png", f"{spec.name}_charuco.pdf"
            record["raster"] = render_png(directory / png_name, config.geometry, config.printing, boards[spec.name])
            record["files"] = {"png": png_name, "pdf": pdf_name}
            manifest["boards"].append(record)
            manifest["pdf_layouts"][pdf_name] = render_pdf(directory / pdf_name, config, (spec,), boards)
        combined = "_".join(spec.name for spec in config.boards) + "_" + config.printing.page_size.upper() + ".pdf"
        manifest["combined_pdf"] = combined
        manifest["pdf_layouts"][combined] = render_pdf(directory / combined, config, config.boards, boards)
        for record in manifest["boards"]:
            record["files"]["combined_pdf"] = combined
            record["sha256"] = {kind: sha256(directory / record["files"][kind]) for kind in ("png", "pdf", "combined_pdf")}
        filenames = {record["files"]["png"] for record in manifest["boards"]} | set(manifest["pdf_layouts"])
        manifest["sha256"] = {filename: sha256(directory / filename) for filename in sorted(filenames)}
        write_json(directory / "board_manifest.json", manifest)
        # Read back JSON too: no reliance on in-memory board objects during verification.
        report = validate_artifacts(directory, load_manifest(directory / "board_manifest.json"))
        report["manifest_sha256"] = sha256(directory / "board_manifest.json")
        write_json(directory / "validation_report.json", report)
        if not report["passed"]:
            raise ValidationError(report)
        target.mkdir(parents=True, exist_ok=True)
        # Individual replacements are atomic; this is not a multi-file transaction.
        # The manifest/report are published last and their hashes detect interrupted sets.
        for filename in [*sorted(filenames), "board_manifest.json", "validation_report.json"]:
            os.replace(directory / filename, target / filename)
        return report
