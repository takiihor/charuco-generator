# Marker generator for camera localization

Generates printable, actual-size markers for camera calibration and for locating objects with a camera: ArUco markers, ChArUco boards, and the complete two-stage set for tank localization on an AGV with a UR10e. Every output is a lossless PNG plus a vector PDF to print at **Actual size / 100%**, with a manifest and a validation report. Generation reads the saved files back and refuses to publish anything that does not decode to the expected IDs and geometry.

**Reading a marker proves identity and a position in the image. It does not prove the printed size is right or that a robot will be placed accurately.** Measure the scale bars on every printed sheet.

## Which marker for which job

| Marker | What it is | Use it for | Command |
| --- | --- | --- | --- |
| **ArUco**, single | One square code; its ID is a number | Coarse identity and position at a distance, e.g. "which tank, roughly where" | `./generate_aruco.sh` |
| **ChArUco board** | Checkerboard with an ArUco code in each white square | Precise position and angle: the corners are located to sub-pixel accuracy, and any one code identifies the board even if the rest is hidden | `./generate_charuco.sh` |
| **Tank boards** | One small ChArUco board per tank, each with its own block of IDs | Tank identity **and** a trusted coordinate (at least 6 inner corners) | `./generate_tanks.sh` |
| **Calibration board** | One large ChArUco board filling a sheet | Calibrating a camera (lens parameters) and hand-eye (camera to robot) | `./generate_calibration.sh` |
| **Two-stage set** | All of the above for the AGV and UR10e, with one ID plan | The whole tank-localization system | `./generate_two_stage.sh` |

Rule of thumb: a single ArUco is robust and readable from far away, but its angle and exact position are weak. A ChArUco board gives a much better position and angle, but needs more pixels per marker cell, so it must be bigger or closer.

## The two-stage tank localization

An AGV carries a fixed right-facing camera and a UR10e with a wrist camera. Positioning at a tank takes two stages:

1. **Coarse:** the AGV camera reads a large marker per tank from 500-800 mm and guides the wrist camera to the tank.
2. **Fine:** the wrist camera reads a small ChArUco board per tank from about 300 mm for the precise coordinate.

```bash
./generate_two_stage.sh        # 16 tanks, into output/two_stage/ (--tanks N for 1-24)
```

| Folder | Camera | Target | Code | IDs |
| --- | --- | --- | --- | --- |
| `stage1_aruco_realsense` | AGV camera (RealSense), 500-800 mm | 150 mm ArUco per tank | `DICT_5X5_100` | tank *n* = ID *n* (1-16) |
| `stage2_charuco_basler_32mm` | Wrist camera (Basler), ~300 mm | 32 x 32 mm ChArUco per tank, 9 corners | `DICT_6X6_250` | tank *n*: 17 + 8(*n*-1) to 24 + 8(*n*-1) |
| `stage2_charuco_realsense_80mm` | Wrist camera (RealSense), ~300 mm | Same boards at 80 x 80 mm, for testing before the Basler arrives | `DICT_6X6_250` | same as the 32 mm set |
| `calibration_stage1` | AGV camera, calibrate at 500-650 mm | 360 x 200 mm board on A3 landscape | `DICT_6X6_250` | 145-166 |
| `calibration_stage2` | Wrist camera (Basler), ~300 mm | 75 x 105 mm board | `DICT_6X6_250` | 167-183 |

`marker_plan.json` lists every set and a per-tank lookup (stage-1 ID, stage-2 board name, stage-2 IDs). Have the robot act only when both stages report the same tank.

**Rules the plan enforces or relies on**

- **No shared codes.** Generation stops before writing anything if two sets that may be seen together share a marker code. Same-size dictionaries count as one family (`DICT_5X5_250` repeats `DICT_5X5_100`'s codes).
- **Install only one stage-2 size.** The 32 mm and 80 mm sets are the same targets at two sizes.
- **Front/Rear overlap.** The default Front/Rear boards (`config/boards.json`) use IDs 1-20 of `DICT_5X5_100`, which clash with stage-1 IDs 1-16. The command warns about this. Never let one camera see both, or change the Front/Rear IDs.
- **Keep the working focus.** Calibrate each camera at the focus it will use; refocusing afterwards invalidates the calibration.

### Sizing a marker for a camera

Each black or white cell of a marker should cover at least **5 camera pixels** to be read, and about **8 or more** for margin and tilted views. Cells per marker side: 7 for a 5x5 code, 8 for 6x6, 9 for 7x7 (each includes the black border).

| Camera and lens | Smallest 4x4 board, 8 px per cell, at 300 mm |
| --- | --- |
| RealSense D435i, 1280 x 720 | about 113 mm |
| Basler a2A2448-23gmBAS (2.74 um pixels) + 12 mm lens | about 23 mm |
| Basler + 16 mm lens | about 18 mm |

At 300 mm with a Basler and 12 mm lens the field of view is about 168 x 140 mm, so the board (plus 5 mm white margin) must also fit in the view with room for positioning error. Use `DICT_6X6_250` or `DICT_7X7_250` rather than 4x4 codes for tank boards: their codes differ by 11 and 17 bits (3 for 4x4), so a misread is far less likely. `DICT_8X8` does not exist in OpenCV; `DICT_7X7` is the largest.

## Quick start

Install and the menu, on Linux or macOS (the launcher creates `.venv` and installs pinned dependencies; Python 3.11+ must exist):

```bash
./generate_charuco.sh            # guided menu for Front/Rear style boards
./generate_aruco.sh              # standalone markers, IDs 1-20, 30 mm
./generate_aruco.sh --ids 1-16 --marker-size-mm 150 --output-dir output/aruco_150mm
./generate_tanks.sh --square-size-mm 8 --white-margin-mm 5      # tank boards, 32 mm
./generate_calibration.sh        # 5 x 7 board of 30 mm squares filling A4, DICT_6X6_250, IDs 0-16
./generate_two_stage.sh          # everything for the two-stage system
```

Every command takes `--output-dir`, `--dpi`, `--page-size` and `--help`. Existing files with different content are refused unless you pass `--clean`, so a stale PDF can never be printed next to a new manifest.

## Check what you generated

```bash
./generate_charuco.sh verify --manifest output/two_stage/stage2_charuco_basler_32mm/board_manifest.json
./generate_charuco.sh validate --board tank07 --input photo.png \
    --manifest output/two_stage/stage2_charuco_basler_32mm/board_manifest.json
```

`verify` re-reads every PNG and PDF and checks hashes, dimensions, IDs and scale bars. `validate` reports which markers and corners a photograph shows, and warns when markers get fewer than 5 pixels per module. Exit codes: 0 pass, 1 detection or artifact failure, 2 configuration or input error.

## Printing

1. Print at **Actual size / 100%**: no Fit, no Shrink, no printer scaling.
2. Measure the **horizontal and vertical** scale bars on each panel with a calibrated caliper. Two bars expose different X and Y scaling. A 1% scale error becomes a 1% distance error with no sign in any detection metric.
3. Mount on a flat, rigid, matte substrate. Keep the white margin. On a plating line use chemical-resistant markers (for example laser-marked anodised aluminium) rather than paper.

## Where to read more

- [Detailed reference](docs/REFERENCE.md): every option, the manifest format, raster rounding, photo validation, SVG/DXF exports, rebuilding boards in your own code, mounting and precision limits.
- Companion projects: `charuco-realsense` (live detection and pose with RealSense), `ur10e-handeye` (hand-eye calibration over RTDE), `ur-pick-place` (vision-guided pick and place).

## Development

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q && python -m ruff check . && python -m mypy
```
