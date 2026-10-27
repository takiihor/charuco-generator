# Precision ChArUco and ArUco generator

Generate distinct Front and Rear references for camera calibration and future industrial localization. The defaults use OpenCV `DICT_5X5_100`, a 5 x 4 chessboard, 20 mm squares and 14 mm markers. Each pattern is nominally **100 x 80 mm**. Front encodes IDs **1-10**; Rear encodes **11-20**. Both have 10 markers and 12 internal ChArUco intersections.

The utility generates lossless PNGs, actual-size **vector PDFs**, a reconstruction manifest, and a machine-readable validation report. It reads the saved files back, reconstructs the boards and validates them before publishing the outputs. Optional SVG, DXF and OpenCV dictionary exports support manufacturing rigid targets. A separate command produces a standard A4 camera calibration board. No robot motion, UR control, hand-eye calibration or placement logic is implemented.

**Successful detection does not establish printed dimensional accuracy or robot placement accuracy. No physical printed-board measurement has been performed for the supplied examples.**

## Install

On Linux or macOS, start the guided terminal menu with:

```bash
./generate_charuco.sh
```

The launcher finds Python 3.11+, creates `.venv` when needed, and installs the pinned runtime dependencies. Later launches check installed package versions locally and skip installation when they match, so a configured environment starts without internet access. It can be invoked from any directory. Python itself must already be installed; the first dependency installation needs internet access. It does not install system packages or require `sudo`.

For one-command generation without the menu:

```bash
./generate_charuco.sh generate                        # Configured Front and Rear IDs
./generate_charuco.sh --select rear --start-id 40 --output-dir output/rear40
./generate_charuco.sh --start-id 20 --output-dir output/set2   # Front 20-29, Rear 30-39
```

With redirected input, a bare `./generate_charuco.sh` retains the original automatic generation behavior. Use `./generate_charuco.sh menu` to request the menu explicitly, or `./generate_charuco.sh menu --output-dir output/project2` to choose another output root.

Pass existing CLI commands or generation options directly:

```bash
./generate_charuco.sh --output-dir output/custom
./generate_charuco.sh generate --config config/boards.json --dpi 300
./generate_charuco.sh validate --input captured_photo.png --board front
./generate_charuco.sh verify --manifest output/board_manifest.json
./generate_charuco.sh --help
```

Relative input/config/output paths passed to the launcher are resolved from the project directory. Use absolute paths for files elsewhere. To select Python when creating a new environment, use `PYTHON_BIN=/path/to/python3.12 ./generate_charuco.sh`. An existing environment is reused; move it aside first if you want to change its interpreter.

For manual installation or Windows, follow the steps below.

Use Python 3.11 or later and an isolated environment. The package uses `opencv-contrib-python-headless==4.11.0.86` (`cv2.__version__ == "4.11.0"`). All dependencies, including OpenCV's wheel version and transitive packages, are pinned in `requirements.txt`; test and lint tooling is pinned separately in `requirements-dev.txt`, so installing the package never drags `pytest` into a production environment.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt        # runtime only
python -m pip install -r requirements-dev.txt    # adds pytest, ruff, mypy
python -m pip check
```

On Windows, activate with `.venv\Scripts\activate`. Install only one OpenCV distribution in the environment; several distributions share the `cv2` namespace. The utility does not require a GUI or `cv2.imshow`.

The implementation explicitly uses `cv2.aruco.CharucoBoard(..., ids)`, `board.generateImage()`, `cv2.aruco.ArucoDetector`, and `cv2.aruco.CharucoDetector`. Because board layout and detector behaviour change across releases, it refuses an untested OpenCV build instead of silently producing different geometry:

```bash
python generate.py generate                        # error: Tested OpenCV 4.11.0 required; found 4.12.0
python generate.py generate --allow-untested-opencv  # runs, and every report carries a warning
```

`--allow-untested-opencv` sets `CHARUCO_ALLOW_UNTESTED_OPENCV=1`; the reports then record `untested_opencv` warnings so a revision built against another OpenCV is never mistaken for a verified one. See the versioned [CharucoBoard API](https://docs.opencv.org/4.11.0/d0/d3c/classcv_1_1aruco_1_1CharucoBoard.html) and [ChArUco detection tutorial](https://docs.opencv.org/4.11.0/df/d4a/tutorial_charuco_detection.html).

## Generate both boards

Run these commands from the project root:

```bash
python generate.py generate --config config/boards.json
python -m pytest -q
```

The default command produces:

```text
src/charuco_generator/        Python implementation (typed, ships py.typed)
config/boards.json            Editable, shared geometry and explicit ID sequences
generate_charuco.sh           Install dependencies and run the CLI
generate_aruco.sh             Generate the standalone markers
generate.py                   Entry point (also charuco-generator after installing)
requirements.txt              Pinned runtime dependencies
requirements-dev.txt          Pinned test, lint and type-check dependencies
tests/                        Configuration, detection, printing, export and CLI checks
output/
  front_charuco.png           Clean pattern and external white quiet margin
  rear_charuco.png
  front_charuco.pdf           Single Front print sheet
  rear_charuco.pdf            Single Rear print sheet
  front_rear_A4.pdf           Both patterns on one A4 page at their actual size
  board_manifest.json         Geometry, ID placement, compatibility, paths and hashes
  validation_report.json      Detection results and physical PDF geometry checks
  vector/                     SVG/DXF/dictionary copies, only when requested
```

PNG files intentionally contain only the pattern and white margin. The print PDFs add identification labels, **two** scale bars and pattern-edge projection marks **outside** the pattern and its white margin; see [Printing and independent measurement](#printing-and-independent-measurement). Print the PDFs for the most reliable dimension handling. No cutting guides or decorative borders are drawn near the pattern.

Output paths in the manifest are relative to the manifest's own directory, so the complete output directory can be moved. The configuration file path and `output_dir` are interpreted relative to the current working directory. Use `--output-dir /desired/path` to choose another destination.

Each invocation validates staged artifacts first. Invalid configuration or failed synthetic validation returns a nonzero exit code and does not publish new print files. Successful publication replaces files individually, with the manifest and report last. It is not a multi-file filesystem transaction: run `verify` after an interrupted publication.

### Leaving an old revision behind is refused

Two different configurations written to the same folder is the one failure mode this tool cannot allow quietly: Front and Rear are identified only by their encoded ID sets, so a leftover `front_charuco.pdf` from an earlier revision can be printed and mounted while the manifest beside it describes a different board. Generation therefore refuses to publish into a folder that still holds artifact-shaped files (`*.png`, `*.pdf`) the requested configuration does not produce, and lists them by name:

```bash
python generate.py generate --output-dir output/revised        # refuses, names the stale files
python generate.py generate --output-dir output/revised --clean  # deletes them first
python generate.py verify --manifest output/revised/board_manifest.json
```

`--clean` removes only files matching the artifact patterns; a `notes.txt` or a mounting drawing beside the print files is left alone. `verify` reports the same condition independently as `unexpected_files` and fails, so a half-replaced folder is caught even when generation was not the last thing to touch it.

## Guided menu

The normal menu asks only:

1. Select Front, Rear, or both (enter numbers or names).
2. Enter the overall pattern size in mm, such as `30x30`, `90x90` or `100x80`.
3. Choose the marker IDs (default: keep configured IDs).
4. Review the result and confirm generation.

The size is the complete black-and-white pattern, excluding the external white margin. A `30x30` mm pattern uses a 3 x 3 grid with 10 mm squares and 7 mm markers; `90x90` uses 3 x 3 with 30 mm squares and 21 mm markers. Grid and marker sizing are automatic. For rectangular patterns, the grid follows the requested proportions with equal square dimensions; matching the configured proportions preserves the configured grid. Dimensions that cannot fit the configured page, dictionary or raster limits are rejected at the size prompt.

Press Enter to accept a default, or Ctrl+C to cancel. Marker IDs default to the configured IDs (Front **1-10**, Rear **11-20**); option **1** chooses the next unused ID block, and option **3** chooses the first ID manually. If the configured IDs do not fit the chosen grid (for example a `30x30` pattern has only 4 markers), the menu reports the mismatch and asks the ID option again. A fresh save folder is chosen automatically, so each menu run preserves earlier sets. Folders are labelled with the IDs actually encoded, collapsing only genuinely consecutive runs: `output/front_1-10_rear_11-20/`, `output/front_28-37_rear_38-47/`, or `output/front_0+5+9/` for a nonconsecutive selection. The source JSON is unchanged; the saved manifest contains the exact configuration used.

For detailed control, run:

```bash
./generate_charuco.sh menu --advanced
```

Advanced mode lets you enter the grid as one value such as `3x3`, square and marker sizes, dictionary, paper, DPI, tile overlap, ID mode and save folder. Invalid entries are corrected at the same prompt. Existing CLI flags remain available for scripted generation.

Unused-ID allocation checks all configured board IDs and every `board_manifest.json` **and** `aruco_manifest.json` under the output root, for the selected dictionary. Keep previous generated folders there to reserve their IDs. It cannot know about printed boards or files stored elsewhere or deleted. Choosing the first ID manually or reusing configured IDs is explicit reuse; review the IDs before printing. A manifest that cannot be read stops allocation, because its IDs can no longer be excluded; the error lists every unreadable file and its cause. `--ignore-unreadable-manifests` allocates around them and records the ignored files as a warning in the session output. Allocation reads files without locking, so run one menu session at a time when allocating new IDs.

## Configure physical dimensions and identities

`config/boards.json` is the complete configuration. `geometry` is shared by every board, ensuring one dictionary and one geometry for Front and Rear. Lengths in this file use millimetres; OpenCV board objects use metres. `printing` contains DPI, page size, external white margin, page edge exclusion and tile overlap. Each `boards` entry has a safe filename name, a unique physical-reference identifier and an explicit ordered ID array.

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
| `tile_overlap_mm` | 0 | Overlap for tiled sheets; 0 refuses to split a board that does not fit one page |

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

Ranges in CLI ID arguments are inclusive. Lists such as `front:9,8,7,6,5,4,3,2,1,0` preserve that exact placement sequence. `--board` on `generate` targets an unqualified ID override; use `--select rear` to generate only Rear, or `--select front rear` to select several boards. Without `--select`, all configured boards are generated. `--start-id` calculates the required count from the layout and assigns consecutive, nonoverlapping ranges in configuration order; it also creates new reference identifiers, rejects IDs outside the dictionary and cannot be combined with `--marker-ids`. Additional options are `--page-margin-mm`, `--border-bits`, `--legacy-pattern` / `--no-legacy-pattern`, `--tile-overlap-mm`, `--clean`, `--export-svg`, `--export-dxf`, `--export-dictionary-yml` and `--allow-untested-opencv`. `--square-length-mm` / `--marker-length-mm` are aliases for the size options. `python generate.py --version` prints the generator version, which is also recorded in every manifest.

Unknown config keys, duplicate names/identifiers, duplicate IDs within or across boards, IDs outside the dictionary, incorrect ID count, nonfinite lengths and marker size greater than or equal to square size are rejected. ID-count errors name both sides of the problem (a 20 x 20 grid needs 200 markers, and `DICT_5X5_100` holds 100 IDs). External white margins must be at least 3 mm. Raster resource limits and a minimum of four pixels per marker module prevent unusable or excessively large requests. These software limits do not establish suitability for a particular camera distance.

### Oversized boards: more pages, then tiles

A board that cannot fit one page with its margins, labels and scale bars produces an explicit error and no print files. If each board fits separately but the group cannot fit one sheet, the combined PDF uses additional pages at the same size. There is **no automatic fit-to-page scaling**.

For boards larger than a sheet of paper, request tiling:

```bash
python generate.py generate --squares-x 10 --squares-y 8 --square-size-mm 30 \
  --marker-size-mm 21 --start-id 0 --dpi 150 --tile-overlap-mm 12 --output-dir output/large
```

Each sheet then carries one overlapping tile of the pattern with its own labels, its own pair of scale bars and a `tile 3/8` note in the panel label, and the manifest records each tile's sub-rectangle of the full pattern. Overlap must be smaller than the printable tile. Assembly at the seams adds positioning error that no software check can bound: a single rigid sheet is the better target, and tiling exists for cases where the required field of view genuinely needs it.

## Printing and independent measurement

1. Open `output/front_rear_A4.pdf` (or the individual sheet) in a PDF viewer. Select the matching paper size, **Actual size / 100%**, one PDF page per paper sheet. Disable Fit, Shrink oversized pages, borderless expansion and printer-driver scaling. Check application and driver settings independently. The PDF requests `PrintScaling=None`, and each page repeats `ACTUAL SIZE / 100% - disable Fit, Shrink and printer scaling`; printer software may still override it.
2. Print in black and white with high contrast. Avoid resampling, screenshots, photo-print modes and lossy conversion. Keep the entire white margin intact. Preserve orientation and the label when cutting for mounting.
3. Every panel carries **two** scale bars measured between tick centre lines: a horizontal one in the band below the pattern and a vertical one in the gutter beside it. Measure **both** with a suitable calibrated caliper. A nominal 50 mm bar is used when it fits, shrinking to 40/30/25/20 mm and otherwise to the largest whole millimetre that fits, and its true length is printed in the annotation under the panel. The pair is what exposes anisotropic (X versus Y) scaling, which a single bar cannot.
4. Also measure several nominal 20 mm squares and the full nominal 100 x 80 mm pattern. Measuring several squares together reduces the influence of an uncertain ink edge on a single-square reading. Four short marks project the pattern's left and right edges across the label and reference bands; line a straightedge up on them to find the pattern's trim line and mounting datum instead of guessing where the pattern corner is.
5. Record the measured lengths, instrument uncertainty, printer settings, substrate and mounting conditions. Repeat measurements after adhesive curing and mounting. Use a flat measurement setup; do not deform the paper with caliper jaws. Establish the acceptable tolerance from the complete localization/placement error budget.
6. If dimensions differ, or X and Y scale differently, correct the printing process and print again. Do not treat camera detection as a dimensional measurement or casually change software geometry to conceal a warped print.

The PDF contains vector chessboard squares and marker modules with nominal dimensions expressed in physical PDF points (`1 mm = 72/25.4 points`). Markers are nominally 14 mm including their border; for the default 5 x 5 code plus one border module, each module is 2 mm. The generator checks the actual saved MediaBox/CropBox, page units, form BBox, placement transformations, vector rectangles and **both** scale bars with `pypdf`, plus the per-tile clip when a board is tiled. PDF numeric serialization is checked to **0.001 point (about 0.000353 mm)** and the printed scale bars to within 5 µm of nominal; these tolerances are file-format checks, not printer or measurement tolerances.

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

`validate` uses `output/board_manifest.json` automatically when present. For another output directory, supply its manifest explicitly. Alternatively, use `--config config/boards.json` to construct geometry directly from a config. A supplied manifest and config are mutually exclusive. The generated PNG checksum selects strict synthetic mode automatically. `--synthetic` forces strict mode; `--photo` forces photograph mode. Passing an ArUco manifest is refused with a message that points at `verify` instead of reporting unrelated configuration errors.

Synthetic mode requires all expected encoded marker IDs, the exact marker count, every internal corner ID, expected image dimensions, the exact aspect ratio, black/white pixel values, clear margins, resolution metadata and marker/corner positions consistent with saved geometry. Generation also checks Front/Rear ID disjointness, saved artifact checksums and PDF geometry. A default board must yield exactly 10 markers and 12 corners. Independent detection of the PNGs proves which IDs are encoded, rather than trusting text labels.

Photograph mode reports all observed IDs, matched/foreign/missing IDs, rejected marker candidates, ChArUco IDs/counts and image coordinates. It permits partial visibility and requires at least one selected-board marker and four noncollinear ChArUco corners for a passing result. This is a detection criterion only, with no pose calculation or metrology certification. `--require-complete` requires the exact complete marker/corner set and rejects extra IDs. A photo containing both references can pass normal photograph mode for a selected board; other-board IDs are reported as foreign. Wrong-board, blank, unreadable and insufficient-corner images fail. Missing expected markers remain explicitly reported even when a partial image passes.

Photographs also report `detection_quality`: the marker side measured in image pixels, divided by the modules per marker, plus the minimum and mean over all decoded markers. Below **5 pixels per module**, corner localisation is no longer trustworthy however well the decode looks, so the report adds an explicit `warnings` entry naming the remedy (move closer, raise resolution, or use larger squares). This describes whether an image is usable; it is not a scale or pose measurement. Reprojection residuals and working distance are deliberately not reported: they require camera intrinsics, which this tool neither measures nor assumes.

Default detector settings stay fixed so synthetic checks compare against exact board geometry. When a real photograph misses markers, tune explicitly and only for photographs:

| Flag | OpenCV setting | Accepted |
| --- | --- | --- |
| `--corner-refinement {none,subpix,contour}` | `cornerRefinementMethod` | `none` keeps geometry checks exact |
| `--min-perimeter-rate` | `minMarkerPerimeterRate` | 0.001 to 0.5 |
| `--max-perimeter-rate` | `maxMarkerPerimeterRate` | 0.001 to 4.0 |
| `--adaptive-thresh-window` | `adaptiveThreshWinSizeMin` | 1 to 2000 px |
| `--min-marker-distance-rate` | `minMarkerDistanceRate` | 0.0 to 4.0 |
| `--polygonal-approx-accuracy-rate` | `polygonalApproxAccuracyRate` | 0.0 to 0.5 |

Values outside the range are rejected rather than passed to OpenCV. `markerBorderBits` always follows the saved geometry, never a flag.

Exit codes: **0** = requested checks passed; **1** = detection/artifact validation failed; **2** = configuration, input or API error; **130** = cancelled. Generation prints a JSON failure report to stderr if staged validation fails. `verify` rechecks every saved PNG/PDF, hash and unexpected file; `validate` checks the supplied image. Manifests are schema-checked on load, so a truncated or hand-edited file names itself and its missing keys instead of failing later with an opaque key error.

To check the Basler camera, acquire a sharp still image using your existing camera software and save it losslessly if possible. Use the final working distance, focus, exposure and lighting; avoid glare, motion blur, saturation and very small marker modules. Capture full and partial views at representative viewing angles. Then run the photo command. A perspective/JPEG simulation in the tests exercises the photo code path; it is not a physical camera experiment.

## Generate standalone ArUco markers

On Linux or macOS, run:

```bash
./generate_aruco.sh
```

This generates exactly **20 standalone markers**, encoding IDs **1 through 20 inclusive**, using `DICT_5X5_100`. Each marker is **30 x 30 mm** including its black border, with a separate 10 mm white margin on every side. The script shares the ChArUco launcher's isolated environment and dependency installation, works from any directory, and runs without prompts.

The default destination is `output/aruco/`:

```text
aruco_01.png ... aruco_20.png    Lossless marker images with clear white margins
aruco_01.pdf ... aruco_20.pdf    Individual actual-size vector print sheets
aruco_1-20_A4.pdf                All markers on multiple A4 sheets
aruco_manifest.json              Dictionary, IDs, dimensions, raster metrics, layouts and hashes
aruco_validation_report.json     Saved-image detection and PDF geometry checks
```

Choose which markers to print, their physical size and the output folder with:

```bash
./generate_aruco.sh --ids 21-60 --marker-size-mm 40 --output-dir output/aruco_40mm
./generate_aruco.sh --dictionary DICT_4X4_50 --ids 0-19 --dpi 300 --page-size A3
./generate_aruco.sh --help
./generate_charuco.sh verify --manifest output/aruco/aruco_manifest.json
```

`--ids` takes comma-separated integers or inclusive ranges (default `1-20`) and is capped before rendering; IDs outside the chosen dictionary are rejected because they cannot be encoded. Additional options are `--white-margin-mm`, `--page-margin-mm`, `--border-bits`, `--clean`, `--export-svg`, `--export-dxf`, `--export-dictionary-yml` and `--allow-untested-opencv`. For manual installation or Windows, use `python generate.py aruco` after installing the dependencies above. Relative launcher paths resolve from the project directory.

The generator reads every saved PNG back and requires detection of exactly its expected ID, then checks the actual PDF vector dimensions before publishing. PNG sampling uses whole pixels per module; the manifest records the effective DPI. PDFs preserve the requested physical size and use the same annotation bands as the ChArUco sheets: ID labels, a horizontal and a vertical scale bar, pattern-edge projection marks, and added pages as needed. Print at **Actual size / 100%** and measure both bars. Repeated runs replace the generated files in the selected directory, and stale sheets from a different ID or size selection are refused or removed with `--clean`, exactly as for boards.

The default standalone markers use the same dictionary and IDs as the default ChArUco boards. ArUco detection identifies matching dictionary/ID pairs identically on either type of print. Marker encoding follows the versioned [OpenCV ArUco generation API](https://docs.opencv.org/4.11.0/d5/dae/tutorial_aruco_detection.html).

## Generate a standard A4 camera calibration board

On Linux or macOS, run:

```bash
./generate_calibration.sh
```

This generates one **ChArUco calibration board filling an A4 sheet**: **5 x 7 squares of 30 mm** (150 x 210 mm pattern), **22.5 mm markers**, `DICT_6X6_250`, marker IDs **0-16**, giving 17 markers and 24 internal corners. That is OpenCV's conventional ChArUco calibration layout, usable for camera intrinsics (`calibrateCameraCharuco`) and robot hand-eye calibration tools such as MoveIt Calibration. The board uses a **different dictionary** from the `DICT_5X5_100` localization references and tank markers, so it can stay in view without ever being decoded as Front, Rear or a tank label.

The module only chooses the geometry: the largest grid of the requested square size that fits one sheet with the standard margins, labels and both scale bars. Generation, verification, photo validation, stale-file protection and the optional exports are the ordinary ChArUco pipeline, so the outputs in `output/calibration/` are the usual `calibration_charuco.png`, `calibration_charuco.pdf`, `calibration_A4.pdf`, `board_manifest.json` and `validation_report.json`. The command prints the exact OpenCV constructor for your calibration program:

```text
OpenCV: cv2.aruco.CharucoBoard((5, 7), 0.03, 0.0225, cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_6X6_250))
```

Other sizes and layouts:

```bash
./generate_calibration.sh --landscape                    # 8 x 4 squares of 30 mm on A4 landscape
./generate_calibration.sh --square-size-mm 20            # 7 x 10 squares: more corners, closer working distance
./generate_calibration.sh --page-size A3                 # 8 x 11 squares of 30 mm
./generate_calibration.sh --squares 4x6 --square-size-mm 35 --output-dir output/calib_35mm
./generate_charuco.sh validate --board calibration --manifest output/calibration/board_manifest.json --input photo.png
```

Additional options are `--marker-size-mm` (default 0.75 x square), `--dictionary`, `--first-id`, `--dpi`, `--white-margin-mm`, `--page-margin-mm`, `--clean`, `--export-svg`, `--export-dxf`, `--export-dictionary-yml` and `--allow-untested-opencv`. An explicit `--squares` grid that does not fit one sheet is rejected rather than scaled or tiled; a calibration target should be one flat piece. For manual installation or Windows, use `python generate.py calibration`.

Choose squares large enough for your working distance: the photo validator warns below 5 image pixels per marker module. An **odd row count** (the default 7) gives the same layout in every OpenCV version; with an even row count, tools built on OpenCV < 4.6 need their legacy-pattern setting, and the command says so. As with every sheet here, print at **Actual size / 100%**, mount the board flat and rigid, and measure both scale bars before trusting the calibration. A uniform print-scale error leaves pixel intrinsics unchanged but scales every estimated translation, including the hand-eye and target poses, by the same factor; unequal X/Y scaling also distorts the intrinsics. Enter the measured square size, not the nominal one, if they differ.

## Vector exports for rigid targets

Paper deforms; a permanent industrial reference is often machined or cut. These exports write into a `vector/` subfolder, are recorded in `manifest["exports"]` with relative paths and covered by the manifest checksums, and are deliberately **not** print files: they never appear as candidates for the stale-artifact check.

```bash
python generate.py generate --export-svg --export-dxf --export-dictionary-yml
python generate.py aruco --ids 1-20 --export-svg --export-dxf
```

| File | Use |
| --- | --- |
| `vector/front.svg`, `vector/rear.svg` | Exact-size vector geometry for laser cutting or printing at another scale |
| `vector/front.dxf`, `vector/rear.dxf` | R12 DXF for CAD/CAM import |
| `vector/DICT_5X5_100.yml` | OpenCV `FileStorage` dictionary for other programs |

The dictionary YAML is written with `Dictionary.writeDictionary()`, the format OpenCV's own calibration tooling expects.

## Reproduce a saved revision

Every manifest stores the exact configuration that produced it. To reprint a revision from its manifest, without keeping the original config file:

```bash
python generate.py generate --from-manifest output/set2/board_manifest.json
python generate.py generate --from-manifest output/set2/board_manifest.json --output-dir output/set2_copy
python generate.py generate --from-manifest output/set2/board_manifest.json --dpi 300
```

`--from-manifest` and `--config` are mutually exclusive. Without `--output-dir`, the board is rewritten into the manifest's own folder; the saved `output_dir` is ignored because it is only meaningful on the machine that produced it. Ordinary overrides still apply, so one saved revision can seed a deliberate change. An ArUco manifest is refused with a pointer to `aruco --ids ...`.

## Reconstruct geometry for future localization

For an installed package, `python -m pip install -e .` makes the library importable; the package ships `py.typed`, so type checkers read its annotations. A source-only program can instead add this repository's `src` directory to `sys.path` before importing. The supplied `generate.py` handles this automatically.

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

The coordinate origin is the **outer top-left pattern corner**, viewed from the printed side. X goes right along columns, Y goes down along rows, Z points into the board (`X cross Y`); the board plane is Z=0. Internal corners start at `(square_length_m, square_length_m, 0)` and have zero-based row-major IDs. The origin excludes margins, labels and page placement. That corner is invisible on a trimmed sheet, which is why each panel prints marks projecting the pattern edges. OpenCV stores object points as float32, so saved coordinates include its numerical rounding.

The modern `legacy_pattern=false` layout is explicitly selected. With an even number of rows, legacy mode places a marker in the upper-left white cell instead. Mixing these layouts changes marker placement; preserve the flag alongside IDs and geometry. Board frame conventions and OpenCV versions also matter when integrating older software.

`board_manifest.json` contains names/identifiers, dictionary, complete marker sequence, row/column placement and corner coordinates, square/marker dimensions in mm and m, pattern dimensions, expected internal corner coordinates, frame convention, environment versions, raster rounding, PDF placement including per-tile sub-rectangles, relative output paths, export paths and SHA-256 checksums. Hashes cover PNG/PDF files and `vector/` exports; the manifest and report exclude recursive self-hashes. The report separately records the manifest checksum. These hashes detect accidental changes, not malicious tampering or physical distortion. Reproducibility tests compare every generated file byte-for-byte for identical configuration and software environment; exact detector floating-point results across different platforms are not promised.

### Dictionary quality is recorded, not assumed

Both manifests carry a `dictionary_quality` block: dictionary capacity, modules per marker, `maxCorrectionBits`, the minimum Hamming distance across the whole dictionary, and the minimum/mean distance **within the IDs actually printed**. Distances are rotation-aware, because the detector matches a candidate under all four rotations, and agree with OpenCV's `maxCorrectionBits = (distance - 1) / 2`. Generation prints it:

```text
Dictionary: capacity 100, 5x5 modules, min Hamming distance 7, distance within the IDs used: 9
```

Higher distance tolerates more bit errors before one ID can be confused with another. It is an encoding statistic only: it says nothing about print quality, camera distance or decoding success, and a bigger dictionary is not automatically better (`DICT_5X5_1000` has a lower minimum distance than `DICT_5X5_100`).

Manifests written by generator 1.0.0 recorded these distances with a bit-unpacking error (for example 6 instead of 7 for `DICT_5X5_100`). `verify` recomputes them and reports the difference as a warning rather than failing such artifacts, which are otherwise unaffected.

## Mounting, identification and precision limits

Keep Front and Rear physically labelled and keep the manifest with the references. Select the board by its encoded ID set, never by image position or a text label alone. Corner IDs overlap between the two boards (both have corner IDs 0-11); **ArUco marker IDs provide the identity**. Do not install duplicate copies of the same ID set in a shared observation area. Changing geometry, ID order, dictionary or legacy mode creates a new reference revision; update the identifier and regenerate its manifest.

Mount the references on **mechanically stable, flat, suitably temperature-resistant structures** with stable attachment to the support fixture. Verify that the complete mounting stack, adhesive, ink and substrate tolerate the actual environment. Keep the targets clear of wear, contamination, glare and mechanical loads, and retain white margins for detection and mounting. Establish the actual board-to-support relationship independently before future localization integration.

Paper is useful for prototyping. Humidity, heat, curling, adhesive shrinkage, printer nonuniformity and damage can change its geometry or its relationship to the fixture. A permanent industrial reference may need an appropriately manufactured rigid target and independent dimensional characterization; substrate material alone does not certify accuracy.

For an eye-in-hand UR10e/Basler system with only 2-3 mm mechanical clearance, detection success is only one part of the error budget. Print geometry, target flatness and mounting, camera intrinsics/distortion, image quality, hand-eye and tool/fixture transforms, robot accuracy/repeatability, structural deflection and thermal drift all affect placement. More detected corners or a low pixel residual cannot certify the available clearance. Independently measure the board and later validate the complete system against the required placement tolerance before relying on it for production.

## Test, lint and CI

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q          # unit, integration and CLI tests
python -m ruff check .       # style and bug-class linting
python -m mypy               # type check of src, tests and generate.py
python generate.py verify --manifest output/board_manifest.json
```

Tests cover dimensions, exact IDs and placement, configuration validation and error messages, identity separation, stale-artifact refusal and `--clean`, synthetic corners, PNG sampling, PDF physical geometry and altered-scale rejection, both scale bars, annotation separation, tiling and tile verification (including boards that only overflow one axis), dictionary-quality values against a brute-force rotation-aware oracle, the A4 calibration board's page fill, dictionary isolation and print-rendered detection, SVG/DXF/YAML exports, alternate layouts/dictionaries/IDs, deterministic byte-for-byte output, manifest reconstruction and schema rejection, failure publication and CLI exit codes. If Poppler is installed, an integration test independently rasterizes the combined vector PDF and detects both encoded boards; the test is skipped when `pdftoppm` is unavailable and the core generator never requires Poppler. Test counts are reported by the test run itself rather than duplicated in this document.

`.github/workflows/ci.yml` runs lint, type checking, the test suite and a real generate/verify cycle on Linux, macOS and Windows for Python 3.11, 3.12 and 3.13, and installs Poppler on Linux and macOS so the vector-PDF rasterization test actually executes instead of silently skipping. A separate job builds the wheel, installs it and runs the `charuco-generator` console script, which is what proves the packaging metadata, `py.typed` and the version single-source are correct.

The MIT licence text is in `LICENSE`.

For visual inspection (optional Poppler installation: `apt install poppler-utils`, or the equivalent for your OS):

```bash
mkdir -p tmp/pdfs
pdftoppm -r 150 -png output/front_rear_A4.pdf tmp/pdfs/combined
pdfinfo output/front_rear_A4.pdf
```

Inspect the rendered page for unclipped labels, white clearance, crisp patterns and both scale bars. A screen rendering, like synthetic marker detection, cannot verify a physical printed scale.
