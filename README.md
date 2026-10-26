# Precision ChArUco board generator

Generate distinct Front and Rear references for camera calibration and future industrial localization. The defaults use OpenCV `DICT_5X5_100`, a 5 x 4 chessboard, 20 mm squares and 14 mm markers. Each pattern is nominally **100 x 80 mm**. Front encodes IDs **0-9**; Rear encodes **10-19**. Both have 10 markers and 12 internal ChArUco intersections.

The utility generates lossless PNGs, actual-size **vector PDFs**, a reconstruction manifest, and a machine-readable validation report. It reads the saved files back, reconstructs the boards and validates them before publishing the outputs. No robot motion, UR control, hand-eye calibration or placement logic is implemented.

**Successful detection does not establish printed dimensional accuracy or robot placement accuracy. No physical printed-board measurement has been performed for the supplied examples.**

## Install

On Linux or macOS, start the guided terminal menu with:

```bash
./start.sh
```

The normal menu asks only:

1. Select Front, Rear, or both (enter numbers or names).
2. Enter the overall pattern size in mm, such as `30x30`, `90x90` or `100x80`.
3. Review the result and confirm generation.

The size is the complete black-and-white pattern, excluding the external white margin. A `30x30` mm pattern uses a 3 x 3 grid with 10 mm squares and 7 mm markers; `90x90` uses 3 x 3 with 30 mm squares and 21 mm markers. Grid and marker sizing are automatic. For rectangular patterns, the grid follows the requested proportions with equal square dimensions; matching the configured proportions preserves the configured grid. Dimensions that cannot fit the configured page, dictionary or raster limits are rejected at the size prompt.

Press Enter to accept a default, or Ctrl+C to cancel. New unused marker IDs and a fresh save folder are chosen automatically. With the supplied configuration, outputs and default 100 x 80 mm size, the first new set uses Front **20-29** and Rear **30-39**. A later run automatically skips saved IDs. Each menu run saves to a separate folder such as `output/front_20-29_rear_30-39/`, preserving earlier sets. The source JSON is unchanged; the saved manifest contains the exact configuration used.

For detailed control, run:

```bash
./start.sh menu --advanced
```

Advanced mode lets you enter the grid as one value such as `3x3`, square and marker sizes, dictionary, paper, DPI, ID mode and save folder. Invalid entries are corrected at the same prompt. Existing CLI flags remain available for scripted generation.

Unused-ID allocation checks all configured board IDs and every `board_manifest.json` under the output root, for the selected dictionary. Keep previous generated folders there to reserve their IDs. It cannot know about printed boards or files stored elsewhere or deleted. Choosing the first ID manually or reusing configured IDs is explicit reuse; review the IDs before printing. A corrupt saved manifest stops automatic allocation until it is repaired. Run one menu session at a time when allocating new IDs.

The launcher finds Python 3.11+, creates `.venv` when needed, and installs the pinned dependencies. Later launches check installed package versions locally and skip installation when they match, so a configured environment starts without internet access. It can be invoked from any directory. Python itself must already be installed; the first dependency installation needs internet access. It does not install system packages or require `sudo`.

For one-command generation without the menu:

```bash
./start.sh generate                                      # Configured Front and Rear IDs
./start.sh --select rear --start-id 40 --output-dir output/rear40
./start.sh --start-id 20 --output-dir output/set2          # Front 20-29, Rear 30-39
```

`--select` controls which boards are generated. `--start-id` calculates the required count from the layout and assigns consecutive, nonoverlapping ranges in configuration order. It also creates new reference identifiers; IDs outside the dictionary are rejected. It does not search saved outputs: use the menu's **new unused IDs** option for that. Explicit `--marker-ids` lists/ranges remain available and cannot be combined with `--start-id`.

With redirected input, a bare `./start.sh` retains the original automatic generation behavior. Use `./start.sh menu` to request the menu explicitly, or `./start.sh menu --output-dir output/project2` to choose another output root. The Python entry point also supports `python generate.py menu`.

Pass existing CLI commands or generation options directly:

```bash
./start.sh --output-dir output/custom
./start.sh generate --config config/boards.json --dpi 300
./start.sh validate --input captured_photo.png --board front
./start.sh verify --manifest output/board_manifest.json
./start.sh --help
```

Relative input/config/output paths passed to the launcher are resolved from the project directory. Use absolute paths for files elsewhere. To select Python when creating a new environment, use `PYTHON_BIN=/path/to/python3.12 ./start.sh`. An existing environment is reused; move it aside first if you want to change its interpreter.

For manual installation or Windows, follow the steps below.

Use Python 3.11 or later and an isolated environment. The example artifacts were executed with CPython 3.12.15 on Linux. All dependencies, including OpenCV's wheel version and transitive packages, are pinned in `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
```

On Windows, activate with `.venv\Scripts\activate`. The package uses `opencv-contrib-python-headless==4.11.0.86` (`cv2.__version__ == "4.11.0"`). Install only one OpenCV distribution in the environment; several distributions share the `cv2` namespace. The utility does not require a GUI or `cv2.imshow`.

The implementation explicitly uses `cv2.aruco.CharucoBoard(..., ids)`, `board.generateImage()`, `cv2.aruco.ArucoDetector`, and `cv2.aruco.CharucoDetector`. It refuses untested OpenCV versions rather than silently changing layout behavior. See the versioned [CharucoBoard API](https://docs.opencv.org/4.11.0/d0/d3c/classcv_1_1aruco_1_1CharucoBoard.html) and [ChArUco detection tutorial](https://docs.opencv.org/4.11.0/df/d4a/tutorial_charuco_detection.html).

## Generate both boards

Run these commands from the project root:

```bash
python generate.py generate --config config/boards.json
python -m pytest -q
```

The default command produces:

```text
src/charuco_generator/        Python implementation
config/boards.json           Editable, shared geometry and explicit ID sequences
output/
  front_charuco.png          Clean pattern and external white quiet margin
  rear_charuco.png
  front_charuco.pdf          Single Front print sheet
  rear_charuco.pdf           Single Rear print sheet
  front_rear_A4.pdf          Both patterns on one A4 page at their actual size
  board_manifest.json        Geometry, ID placement, compatibility, paths and hashes
  validation_report.json     Detection results and physical PDF geometry checks
tests/                       Configuration, detection, printing and CLI checks
requirements.txt
generate.py
start.sh                     Install dependencies and run the CLI
```

PNG files intentionally contain only the pattern and white margin. The print PDFs include identification labels and a 50 mm reference line **outside** the pattern and its white margin. Print the PDFs for the most reliable dimension handling. No cutting guides or decorative borders are drawn near the pattern.

Output paths in the manifest are relative to the manifest's own directory, so the complete output directory can be moved. The configuration file path and `output_dir` are interpreted relative to the current working directory. Use `--output-dir /desired/path` to choose another destination.

Each invocation validates staged artifacts first. Invalid configuration or failed synthetic validation returns a nonzero exit code and does not publish new print files. Successful publication replaces files individually, with the manifest and report last. It is not a multi-file filesystem transaction: run `verify` after an interrupted publication. Stale files from an earlier configuration are not removed; use a fresh output directory for a new board revision.

## Configure physical dimensions and identities

`config/boards.json` is the complete configuration. `geometry` is shared by every board, ensuring one dictionary and one geometry for Front and Rear. Lengths in this file use millimetres; OpenCV board objects use metres. `printing` contains DPI, page size, external white margin and page edge exclusion. Each `boards` entry has a safe filename name, a unique physical-reference identifier and an explicit ordered ID array.

| Setting | Default | Meaning |
| --- | --- | --- |
| `dictionary` | `DICT_5X5_100` | Predefined OpenCV ArUco dictionary |
| `squares_x`, `squares_y` | 5, 4 | Chessboard squares, not internal corners |
| `square_length_mm` | 20 | Physical square side |
| `marker_length_mm` | 14 | Marker side including its black border |
| `legacy_pattern` | false | Explicit modern layout; top-left square is black |
| `border_bits` | 1 | Marker black border width in modules; detector uses the same value |
| `dpi` | 600 | Target raster sampling density |
| `page_size` | `A4` | `A4`, `A3`, `LETTER`, or `WIDTHxHEIGHT` in mm |
| `white_margin_mm` | 10 | Unmarked space immediately around each pattern |
| `page_margin_mm` | 10 | Assumed nonprintable page edge exclusion; confirm for your printer |

Pattern width is `squares_x * square_length_mm`; height is `squares_y * square_length_mm`. Marker count is `floor(squares_x * squares_y / 2)` for the supported layouts. Internal corner count is `(squares_x - 1) * (squares_y - 1)`. Change the ID arrays as well when changing the number of squares.

Examples of command-line overrides:

```bash
python generate.py generate --config config/boards.json \
  --square-size-mm 18 --marker-size-mm 12 \
  --dpi 600 --page-size A4 --white-margin-mm 10 \
  --output-dir output/smaller

python generate.py generate --config config/boards.json \
  --squares-x 6 --squares-y 4 \
  --dictionary DICT_5X5_100 \
  --marker-ids front:20-31 --marker-ids rear:32-43 \
  --page-size A3 --output-dir output/wider

python generate.py generate --config config/boards.json \
  --board rear --marker-ids 40-49 --output-dir output/revised_ids
```

Ranges in CLI ID arguments are inclusive. Lists such as `front:9,8,7,6,5,4,3,2,1,0` preserve that exact placement sequence. `--board` on `generate` targets an unqualified ID override; use `--select rear` to generate only Rear, or `--select front rear` to select several boards. Without `--select`, all configured boards are generated. Additional options are `--page-margin-mm`, `--border-bits`, `--legacy-pattern` and `--no-legacy-pattern`. `--square-length-mm` / `--marker-length-mm` are aliases for the size options. `python generate.py generate --help` lists all options.

Unknown config keys, duplicate names/identifiers, duplicate IDs within or across boards, IDs outside the dictionary, incorrect ID count, nonfinite lengths and marker size greater than or equal to square size are rejected. External white margins must be at least 3 mm. Raster resource limits and a minimum of four pixels per marker module prevent unusable or excessively large requests. These software limits do not establish suitability for a particular camera distance.

Boards that cannot fit the page with their margins, reference lines and labels produce an explicit warning/error and no print files. If each board fits separately but the group cannot fit one sheet, the combined PDF uses additional pages at the same size. There is **no automatic fit-to-page scaling**.

## Printing and independent measurement

1. Open `output/front_rear_A4.pdf` (or the individual sheet) in a PDF viewer. Select the matching paper size, **Actual size / 100%**, one PDF page per paper sheet. Disable Fit, Shrink oversized pages, borderless expansion and printer-driver scaling. Check application and driver settings independently. The PDF requests `PrintScaling=None`; printer software may still override it.
2. Print in black and white with high contrast. Avoid resampling, screenshots, photo-print modes and lossy conversion. Keep the entire white margin intact. Preserve orientation and the label when cutting for mounting.
3. Use a suitable calibrated caliper or measuring instrument to check the **50 mm line between the tick centre lines**. Also measure several nominal 20 mm squares in both X and Y, and the full nominal 100 x 80 mm pattern. Measuring several squares together reduces the influence of an uncertain ink edge on a single-square reading. The line alone cannot rule out anisotropic scaling or local distortion.
4. Record the measured lengths, instrument uncertainty, printer settings, substrate and mounting conditions. Repeat measurements after adhesive curing and mounting. Use a flat measurement setup; do not deform the paper with caliper jaws. Establish the acceptable tolerance from the complete localization/placement error budget.
5. If dimensions differ or X and Y scale differently, correct the printing process and print again. Do not treat camera detection as a dimensional measurement or casually change software geometry to conceal a warped print.

The PDF contains vector chessboard squares and marker modules with nominal dimensions expressed in physical PDF points (`1 mm = 72/25.4 points`). Markers are nominally 14 mm including their border; for the default 5 x 5 code plus one border module, each module is 2 mm. The generator checks the actual saved MediaBox/CropBox, page units, form BBox, placement transformations, vector rectangles and 50 mm reference lines with `pypdf`. PDF numeric serialization is checked to **0.001 point (about 0.000353 mm)**; this tolerance is a file-format check, not a printer or measurement tolerance.

### Raster rounding

A 20 mm square at exactly 600 DPI would require 472.440944... pixels. This utility uses **472 pixels per square**, keeping identical square sampling in X and Y and an exact 5:4 pattern aspect ratio. For the defaults:

| Quantity | Value |
| --- | --- |
| Pattern raster | 2360 x 1888 pixels |
| White margin | 236 pixels per side, nominally 10 mm |
| Complete PNG | 2832 x 2360 pixels |
| Effective pattern resolution at 100 x 80 mm | 599.44 DPI |
| Physical pixel pitch at that size | about 0.042373 mm |
| Pattern if incorrectly forced to exactly 600 DPI | 99.906667 x 79.925333 mm |

The PNG metadata records the effective DPI; PNG's integer pixels-per-metre field introduces a further small metadata rounding. Marker edges and module widths in the PNG remain pixel-quantized; nominal 14 mm cannot always map to an integer pixel count. The manifest records target/effective DPI, pixel dimensions, square rounding, pixel pitch, margins and dimensions if interpreted at the target DPI. The PDFs are independently drawn vectors using the same dictionary bits and board layout, so their physical geometry does not inherit the PNG sampling error. Always use the measured physical print as evidence of its actual dimensions.

## Validate images and camera photographs

```bash
python generate.py validate --input output/front_charuco.png --board front
python generate.py validate --input output/rear_charuco.png --board rear

python generate.py validate --input captured_photo.png --board front \
  --manifest output/board_manifest.json --report output/photo_validation.json

python generate.py verify --manifest output/board_manifest.json
```

`validate` uses `output/board_manifest.json` automatically when present. For another output directory, supply its manifest explicitly. Alternatively, use `--config config/boards.json` to construct geometry directly from a config. A supplied manifest and config are mutually exclusive. The generated PNG checksum selects strict synthetic mode automatically. `--synthetic` forces strict mode; `--photo` forces photograph mode.

Synthetic mode requires all expected encoded marker IDs, the exact marker count, every internal corner ID, expected image dimensions, the exact aspect ratio, black/white pixel values, clear margins, resolution metadata and marker/corner positions consistent with saved geometry. Generation also checks Front/Rear ID disjointness, saved artifact checksums and PDF geometry. A default board must yield exactly 10 markers and 12 corners. Independent detection of the PNGs proves which IDs are encoded, rather than trusting text labels.

Photograph mode reports all observed IDs, matched/foreign/missing IDs, rejected marker candidates, ChArUco IDs/counts and image coordinates. It permits partial visibility and requires at least one selected-board marker and four noncollinear ChArUco corners for a passing result. This is a detection criterion only, with no pose calculation or metrology certification. `--require-complete` requires the exact complete marker/corner set and rejects extra IDs. A photo containing both references can pass normal photograph mode for a selected board; other-board IDs are reported as foreign. Wrong-board, blank, unreadable and insufficient-corner images fail. Missing expected markers remain explicitly reported even when a partial image passes.

Exit codes: **0** = requested checks passed; **1** = detection/artifact validation failed; **2** = configuration, input or API error. Generation prints a JSON failure report to stderr if staged validation fails. `verify` rechecks every saved PNG/PDF and hash; `validate` checks the supplied image.

To check the Basler camera, acquire a sharp still image using your existing camera software and save it losslessly if possible. Use the final working distance, focus, exposure and lighting; avoid glare, motion blur, saturation and very small marker modules. Capture full and partial views at representative viewing angles. Then run the photo command. A perspective/JPEG simulation in the tests exercises the photo code path; it is not a physical camera experiment.

## Reconstruct geometry for future localization

For an installed package, `python -m pip install -e .` makes the library importable. A source-only program can instead add this repository's `src` directory to `sys.path` before importing. The supplied `generate.py` handles this automatically.

```python
import json
from pathlib import Path
import sys

# Omit this line if the package is installed. Run from the repository root.
sys.path.insert(0, str(Path("src").resolve()))

import cv2
from charuco_generator import reconstruct_board

manifest = json.loads(Path("output/board_manifest.json").read_text())
front = reconstruct_board(manifest, "front")
rear = reconstruct_board(manifest, "rear")

image = cv2.imread("captured_photo.png", cv2.IMREAD_GRAYSCALE)
detector = cv2.aruco.CharucoDetector(front)  # default border_bits=1
corners, corner_ids, marker_corners, marker_ids = detector.detectBoard(image)
object_corners_m = front.getChessboardCorners()
# For custom border_bits, set DetectorParameters.markerBorderBits as in validation.py.
```

`reconstruct_board()` verifies the saved configuration fingerprint, OpenCV version, ID sequence, marker placements, corner coordinates and compatibility settings against the rebuilt board. The manifest stores all constructor arguments, explicit IDs, actual OpenCV float32 object points and the layout flag. Another program can implement the same construction directly:

```python
import numpy as np

entry = manifest["boards"][0]
g = entry["geometry"]
dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, g["dictionary"]))
board = cv2.aruco.CharucoBoard(
    (g["squares_x"], g["squares_y"]),
    g["square_length_mm"] / 1000.0,
    g["marker_length_mm"] / 1000.0,
    dictionary,
    np.asarray(entry["marker_ids"], dtype=np.int32),
)
board.setLegacyPattern(g["legacy_pattern"])
```

The coordinate origin is the **outer top-left pattern corner**, viewed from the printed side. X goes right along columns, Y goes down along rows, Z points into the board (`X cross Y`); the board plane is Z=0. Internal corners start at `(square_length_m, square_length_m, 0)` and have zero-based row-major IDs. The origin excludes margins, labels and page placement. OpenCV stores object points as float32, so saved coordinates include its numerical rounding.

The modern `legacy_pattern=false` layout is explicitly selected. With an even number of rows, legacy mode places a marker in the upper-left white cell instead. Mixing these layouts changes marker placement; preserve the flag alongside IDs and geometry. Board frame conventions and OpenCV versions also matter when integrating older software.

`board_manifest.json` contains names/identifiers, dictionary, complete marker sequence, row/column placement and corner coordinates, square/marker dimensions in mm and m, pattern dimensions, expected internal corner coordinates, frame convention, environment versions, raster rounding, PDF placement, relative output paths and SHA-256 checksums. Hashes cover PNG/PDF files; the manifest and report exclude recursive self-hashes. The report separately records the manifest checksum. These hashes detect accidental changes, not malicious tampering or physical distortion. Reproducibility tests compare every generated file byte-for-byte for identical configuration and software environment; exact detector floating-point results across different platforms are not promised.

## Mounting, identification and precision limits

Keep Front and Rear physically labelled and keep the manifest with the references. Select the board by its encoded ID set, never by image position or a text label alone. Corner IDs overlap between the two boards (both have corner IDs 0-11); **ArUco marker IDs provide the identity**. Do not install duplicate copies of the same ID set in a shared observation area. Changing geometry, ID order, dictionary or legacy mode creates a new reference revision; update the identifier and regenerate its manifest.

Mount the references on **mechanically stable, flat, suitably temperature-resistant structures** with stable attachment to the support fixture. Verify that the complete mounting stack, adhesive, ink and substrate tolerate the actual environment. Keep the targets clear of wear, contamination, glare and mechanical loads, and retain white margins for detection and mounting. Establish the actual board-to-support relationship independently before future localization integration.

Paper is useful for prototyping. Humidity, heat, curling, adhesive shrinkage, printer nonuniformity and damage can change its geometry or its relationship to the fixture. A permanent industrial reference may need an appropriately manufactured rigid target and independent dimensional characterization; substrate material alone does not certify accuracy.

For an eye-in-hand UR10e/Basler system with only 2-3 mm mechanical clearance, detection success is only one part of the error budget. Print geometry, target flatness and mounting, camera intrinsics/distortion, image quality, hand-eye and tool/fixture transforms, robot accuracy/repeatability, structural deflection and thermal drift all affect placement. More detected corners or a low pixel residual cannot certify the available clearance. Independently measure the board and later validate the complete system against the required placement tolerance before relying on it for production.

## Test and inspect the outputs

```bash
python -m pytest -q
python generate.py verify --manifest output/board_manifest.json
```

Tests cover dimensions, exact IDs and placement, configuration validation, identity separation, synthetic corners, PNG sampling, PDF physical geometry and altered-scale rejection, annotation separation, alternate layouts/dictionaries/IDs, deterministic output, manifest reconstruction, failure publication and CLI exit codes. If Poppler is installed, an integration test independently rasterizes the combined vector PDF and detects both encoded boards. The test is explicitly skipped when `pdftoppm` is unavailable; the core generator uses no Poppler dependency.

The supplied example run completed **73 tests with no failures or skips**, passed `pip check` and artifact verification, and detected 10 expected markers plus 12 internal corners in each generated PNG. All three PDF sheets were rendered with Poppler and visually inspected. These checks used the environment recorded in the manifest; they did not include a physical print or real camera capture.

For visual inspection (optional Poppler installation: `apt install poppler-utils`, or the equivalent for your OS):

```bash
mkdir -p tmp/pdfs
pdftoppm -r 150 -png output/front_rear_A4.pdf tmp/pdfs/combined
pdfinfo output/front_rear_A4.pdf
```

Inspect the rendered page for unclipped labels, white clearance, crisp patterns and reference lines. A screen rendering, like synthetic marker detection, cannot verify a physical printed scale.
