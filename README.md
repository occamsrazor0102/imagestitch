# MRI Ribbon Stitcher

Stitch a folder of MRI slice JPEGs (or PNG/TIFF/BMP) into one large "ribbon"
image — a grid contact sheet you can scroll through in any image viewer instead
of opening 200 files one at a time.

Everything runs locally. No image ever leaves your machine.

## Run it

**Windows:** double-click **`MRI Ribbon Stitcher.pyw`**, or from a terminal:

```
python mri_ribbon_stitcher.py
```

**macOS:** install Python 3 from https://python.org (the python.org installer
includes tkinter; Homebrew users need `brew install python-tk`), then:

```
pip3 install pillow
python3 mri_ribbon_stitcher.py
```

Shortcuts follow the platform: Ctrl+O/E/P/Z on Windows, Cmd on Mac.

Requires Python 3.9+ with Pillow (`pip install pillow`). tkinter ships with
standard Python on Windows.

## How to use

1. **Add Files…** (Ctrl+O) or **Add Folder…** — slices load in natural filename
   order, so `slice2.jpg` correctly comes before `slice10.jpg`.
2. **Set the order** — drag a row with the mouse, use ▲/▼ (Alt+↑/↓), Top/Bottom,
   **Reverse** (handy when the scanner numbered slices back-to-front), or
   **Sort A→Z** to reset. **Ctrl+Z** undoes any list change (30 steps).
3. **Pick a layout** — choose a preset (for 200 slices: 1×200, 2×100, 4×50,
   5×40, 10×20, … or the near-square 15×14), or type your own columns/rows.
   With *Auto-fit* on, the other dimension always adjusts to hold every slice.
   Choose whether slices flow *across then down* or *down then across*.
4. Optional: gap between slices, background color, slice numbering, output
   scale % (lower it if the full-size image is too large).
5. **Preview** (Ctrl+P) shows a scaled-down composite (click it, then scroll
   with the arrow keys or mouse wheel; Shift+wheel scrolls sideways).
   **Export…** (Ctrl+E) writes the full-quality image with a progress bar;
   **Esc** or the Cancel button stops it. The slice list is locked while a job
   runs so the exported order always matches what you see.

Accessibility notes: everything is keyboard-operable and fonts are ≥11 pt.
tkinter (Python's built-in GUI toolkit) is not visible to screen readers,
however — if you need NVDA/JAWS/Narrator support, ask for the Qt or browser
version; the stitching core is reusable as-is.

## Output formats

- **PNG** (default) — lossless, no size limits. Use this for long ribbons.
- **JPEG** — capped at 65,500 px per side by the format itself; the app warns
  you and suggests PNG when a layout exceeds that (a 1×200 ribbon usually does).
- **TIFF** — lossless alternative.

## Tests

```
python -m unittest discover -s tests
```

## Package as a standalone app (optional)

```
pip install pyinstaller
pyinstaller --onefile --windowed --name "MRI Ribbon Stitcher" mri_ribbon_stitcher.py
```

PyInstaller cannot cross-compile: run the command above **on Windows to get the
.exe** and **on a Mac to get the .app** (output lands in `dist/`). On macOS,
Gatekeeper blocks unsigned apps on first launch — right-click the app,
choose *Open*, and confirm. Verify any packaged build with:

```
"dist/MRI Ribbon Stitcher" --selftest && echo OK
```
