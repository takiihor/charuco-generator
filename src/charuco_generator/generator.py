"""Generate into staging, validate saved files, then publish successful artifacts."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from . import __version__
from .board import canonical_hash, create_board, environment, layout_record, sha256
from .config import Config, ConfigError, dictionary_quality
from .export import VECTOR_DIR, export_board_files, export_dictionary
from .render import plan_pages, raster_metrics, render_pdf, render_png
from .sheet import combined_pdf_name, publish
from .validation import (
    ValidationError,
    expected_artifacts,
    load_manifest,
    unexpected_files,
    validate_artifacts,
)


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def clear_stale(target: Path, keep: set[str], clean: bool, *, remove: bool = True) -> None:
    """Refuse to publish next to artifacts of a different configuration.

    A leftover ``*_charuco.pdf`` keeps its own name but no longer matches the manifest about to be
    written, so a user could print the wrong sheet believing it was the verified one.
    """
    stale = unexpected_files(target, keep)
    if not stale:
        return
    if not clean:
        raise ConfigError(
            f"{target} already holds {len(stale)} artifact file(s) this configuration does not produce: "
            f"{', '.join(stale)}. Remove them, choose another --output-dir, or pass --clean to delete them")
    if remove:
        for name in stale:
            (target / name).unlink()


def generate(config: Config, output_dir: str | Path | None = None, *, clean: bool = False,
             svg: bool = False, dxf: bool = False, dictionary_file: bool = False) -> dict:
    config.validate()
    # Fail before rendering or publishing when dimensions/resources cannot be supported.
    plan_pages(config, config.boards)
    raster_metrics(config.geometry, config.printing)
    target = Path(config.output_dir if output_dir is None else output_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    clear_stale(target, expected_artifacts(config), clean, remove=False)
    with TemporaryDirectory(prefix=".charuco-staging-", dir=target.parent) as staging:
        directory = Path(staging)
        manifest: dict[str, Any] = {
            "schema_version": 1, "generator_version": __version__, "environment": environment(),
            "configuration": config.to_dict(), "configuration_sha256": canonical_hash(config.to_dict()),
            "path_convention": "All file paths are relative to the directory containing this manifest",
            "print_geometry": "Vector forms in PDF points (72 points/inch); no fit-to-page transformation",
            "printing": {"page_size": config.printing.page_size, "scale": 1.0,
                         "viewer_print_scaling": "None", "physical_print_verified": False},
            "boards": [], "pdf_layouts": {}, "sha256": {}, "exports": [],
            "dictionary_quality": dictionary_quality(
                config.geometry.dictionary, [i for spec in config.boards for i in spec.marker_ids]),
            "validation_report": "validation_report.json",
            "checksum_scope": "PNG and PDF artifacts. Manifest and report are excluded to avoid recursive checksums;"
                              " report includes manifest checksum.",
        }
        boards = {spec.name: create_board(config.geometry, spec) for spec in config.boards}
        for spec in config.boards:
            record = layout_record(config.geometry, spec, boards[spec.name])
            png_name, pdf_name = f"{spec.name}_charuco.png", f"{spec.name}_charuco.pdf"
            record["raster"] = render_png(directory / png_name, config.geometry, config.printing, boards[spec.name])
            record["files"] = {"png": png_name, "pdf": pdf_name}
            manifest["boards"].append(record)
            manifest["pdf_layouts"][pdf_name] = render_pdf(directory / pdf_name, config, (spec,), boards)
        combined = combined_pdf_name([spec.name for spec in config.boards],
                                     config.printing.page_size, config.to_dict())
        manifest["combined_pdf"] = combined
        manifest["pdf_layouts"][combined] = render_pdf(directory / combined, config, config.boards, boards)
        for record in manifest["boards"]:
            record["files"]["combined_pdf"] = combined
            record["sha256"] = {kind: sha256(directory / record["files"][kind]) for kind in ("png", "pdf", "combined_pdf")}
        filenames = {record["files"]["png"] for record in manifest["boards"]} | set(manifest["pdf_layouts"])
        exports = export_board_files(directory, config, boards, svg=svg, dxf=dxf)
        if dictionary_file:
            exports.append(export_dictionary(directory / VECTOR_DIR / f"{config.geometry.dictionary}.yml",
                                             config.geometry.dictionary))
        manifest["exports"] = sorted(exports)
        filenames |= set(manifest["exports"])
        manifest["sha256"] = {filename: sha256(directory / filename) for filename in sorted(filenames)}
        write_json(directory / "board_manifest.json", manifest)
        # Read back JSON too: no reliance on in-memory board objects during verification.
        report = validate_artifacts(directory, load_manifest(directory / "board_manifest.json"))
        report["manifest_sha256"] = sha256(directory / "board_manifest.json")
        write_json(directory / "validation_report.json", report)
        if not report["passed"]:
            raise ValidationError(report)
        clear_stale(target, expected_artifacts(config), clean)
        publish(directory, target, sorted(filenames), ["board_manifest.json", "validation_report.json"])
        return report
