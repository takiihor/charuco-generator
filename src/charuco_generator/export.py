"""Vector exports: SVG and DXF drawings of the exact pattern, plus dictionary dumps.

These files carry the same millimetre geometry as the print PDFs, for CAD, laser or CNC work
around a printed board. They are not a substitute for the PDF when printing at actual size.
"""
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import cv2

from .config import dictionary_for
from .render import pattern_rectangles_mm

DXF_VERSION = "AC1009"  # AutoCAD R12: the widest-importable dialect, closed POLYLINEs only.
#: Subdirectory of an artifact set that holds CAD exports, so they cannot be confused with print files.
VECTOR_DIR = "vector"


def export_svg(path: Path, size_mm: tuple[float, float],
               rectangles: list[tuple[float, float, float, float]], title: str) -> None:
    """One black rectangle per vector element, in millimetres, origin at the pattern top-left."""
    width, height = size_mm
    svg = ElementTree.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "version": "1.1",
        "width": f"{width:.6f}mm", "height": f"{height:.6f}mm",
        "viewBox": f"0 0 {width:.6f} {height:.6f}",
    })
    ElementTree.SubElement(svg, "title").text = title
    ElementTree.SubElement(svg, "rect", {"x": "0", "y": "0", "width": f"{width:.6f}",
                                         "height": f"{height:.6f}", "fill": "#ffffff"})
    layer = ElementTree.SubElement(svg, "g", {"id": "pattern", "fill": "#000000"})
    for x, y, w, h in rectangles:
        ElementTree.SubElement(layer, "rect", {"x": f"{x:.6f}", "y": f"{height - y - h:.6f}",
                                               "width": f"{w:.6f}", "height": f"{h:.6f}"})
    path.write_bytes(ElementTree.tostring(svg, encoding="utf-8", xml_declaration=True))


def _dxf_pairs(entities: list[tuple[int, object]]) -> str:
    return "".join(f"{code}\n{value}\n" for code, value in entities)


def export_dxf(path: Path, rectangles: list[tuple[float, float, float, float]], layer: str = "PATTERN") -> None:
    """Closed polylines in millimetre units, y-up like the PDF, on one named layer."""
    entities: list[tuple[int, object]] = [
        (0, "SECTION"), (2, "HEADER"), (9, "$ACADVER"), (1, DXF_VERSION),
        (9, "$INSUNITS"), (70, 4),  # 4 = millimetres
        (0, "ENDSEC"), (0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER"), (70, 1),
        (0, "LAYER"), (2, layer), (70, 0), (62, 7), (6, "CONTINUOUS"), (0, "ENDTAB"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"),
    ]
    for x, y, w, h in rectangles:
        entities += [(0, "POLYLINE"), (8, layer), (66, 1), (70, 1)]
        for corner in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
            entities += [(0, "VERTEX"), (8, layer), (10, f"{corner[0]:.6f}"),
                         (20, f"{corner[1]:.6f}"), (30, "0")]
        entities += [(0, "SEQEND"), (8, layer)]
    entities += [(0, "ENDSEC"), (0, "EOF")]
    path.write_text(_dxf_pairs(entities), encoding="ascii")


def export_board_files(directory: Path, config, boards: dict, *, svg: bool, dxf: bool) -> list[str]:
    """Vector files for one generated board set; returns names relative to the artifact directory."""
    if not (svg or dxf):
        return []
    folder = directory / VECTOR_DIR
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for spec in config.boards:
        rectangles = pattern_rectangles_mm(config.geometry, spec, boards[spec.name])
        stem = folder / spec.name
        if svg:
            export_svg(stem.with_suffix(".svg"), config.geometry.pattern_mm, rectangles,
                       f"ChArUco {spec.identifier} {config.geometry.dictionary}")
            written.append(f"{VECTOR_DIR}/{spec.name}.svg")
        if dxf:
            export_dxf(stem.with_suffix(".dxf"), rectangles)
            written.append(f"{VECTOR_DIR}/{spec.name}.dxf")
    return sorted(written)


def export_dictionary(path: Path, name: str) -> str:
    """OpenCV's own ``nmarkers``/``marker_0`` YAML, loadable with cv2.aruco on the robot side."""
    path.parent.mkdir(parents=True, exist_ok=True)
    storage = cv2.FileStorage(str(path), cv2.FILE_STORAGE_WRITE)
    try:
        cv2.aruco.Dictionary.writeDictionary(dictionary_for(name), storage)
    finally:
        storage.release()
    return f"{path.parent.name}/{path.name}"
