"""MRI Ribbon Stitcher — tkinter GUI.

Stitches a stack of JPEG/PNG slices into one large grid ("ribbon") image.
Run:  python mri_ribbon_stitcher.py   (or double-click "MRI Ribbon Stitcher.pyw")
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, ttk

from PIL import Image, ImageTk

import stitcher_core as core

IMAGE_EXTS = {".jpg", ".jpeg", ".jfif", ".jpe", ".png", ".bmp", ".tif", ".tiff", ".webp"}
PREVIEW_MAX_SIDE = 1500  # longest side of the preview composite, in px
DRAG_THRESHOLD_PX = 5    # vertical movement before a press becomes a drag-reorder
UNDO_DEPTH = 30
MAC = sys.platform == "darwin"
ACCEL = "Cmd" if MAC else "Ctrl"  # shortcut name shown on button labels


def _wheel_steps(event) -> int:
    """Scroll units for a <MouseWheel> event, cross-platform.

    Windows reports delta in multiples of 120; macOS reports small raw values
    (so //120 would floor small upward scrolls to zero there).
    """
    if MAC:
        return -event.delta
    return -event.delta // 120


class RibbonApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MRI Ribbon Stitcher")
        self.minsize(900, 560)
        w = min(1150, self.winfo_screenwidth() - 60)
        h = min(780, self.winfo_screenheight() - 100)
        self.geometry(f"{w}x{h}")
        # ttk widgets take their fonts from the named fonts, not the option
        # database, so both must be set for a readable >= 11pt UI. Keep each
        # platform's own font family; only enforce a minimum size (a negative
        # Tk font size means pixels — hence abs()).
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            f = tkfont.nametofont(name)
            f.configure(size=max(abs(f.actual("size")), 11))
        self.option_add("*Font", "TkDefaultFont")

        self.files: list[Path] = []            # current stitch order
        self.size_cache: dict[Path, tuple[int, int]] = {}
        self.presets: list[tuple[int, int]] = []
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.msgq: queue.Queue = queue.Queue()
        self.preview_photo = None              # keep a reference or Tk drops it
        self.last_export: Path | None = None
        self._syncing_grid = False             # guard against spinbox trace loops
        self._drag_index: int | None = None
        self._drag_y: int = 0
        self._drag_armed = False
        self._undo_stack: list[list[Path]] = []
        self._edit_buttons: list[ttk.Button] = []

        self._build_ui()
        self._bind_keys()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(60, self._poll_queue)
        self._update_summary()

    def report_callback_exception(self, exc, val, tb):
        """Surface any uncaught GUI exception — the .pyw launcher has no console."""
        detail = "".join(traceback.format_exception(exc, val, tb))
        try:
            self.set_status(f"Unexpected error: {val}")
            messagebox.showerror(
                "Unexpected error",
                f"{val}\n\nPlease report this. Technical detail:\n{detail[-900:]}")
        except Exception:
            pass  # never let error reporting raise a second error

    # ------------------------------------------------------------------ UI --

    def _build_ui(self):
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=3)
        root.columnconfigure(1, weight=2)
        root.rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.configure("Warn.TLabel", foreground="#b00000")

        self._build_file_panel(root)
        self._build_options_panel(root)
        self._build_bottom_bar()

    def _build_file_panel(self, parent):
        panel = ttk.LabelFrame(parent, text="Slices (stitch order, top = first)", padding=6)
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)

        box_frame = ttk.Frame(panel)
        box_frame.grid(row=0, column=0, sticky="nsew")
        box_frame.rowconfigure(0, weight=1)
        box_frame.columnconfigure(0, weight=1)
        self.listbox = tk.Listbox(box_frame, selectmode="extended", activestyle="dotbox")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(box_frame, orient="vertical", command=self.listbox.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.bind("<Button-1>", self._drag_start)
        self.listbox.bind("<B1-Motion>", self._drag_motion)
        self.listbox.bind("<ButtonRelease-1>", self._drag_end)

        btns = ttk.Frame(panel)
        btns.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        row1 = ttk.Frame(btns)
        row1.pack(fill="x")
        row2 = ttk.Frame(btns)
        row2.pack(fill="x", pady=(4, 0))

        def edit_btn(parent_, text, command, **pack):
            b = ttk.Button(parent_, text=text, command=command)
            b.pack(side="left", **pack)
            self._edit_buttons.append(b)
            return b

        edit_btn(row1, f"Add Files…  ({ACCEL}+O)", self.add_files)
        edit_btn(row1, "Add Folder…", self.add_folder, padx=4)
        edit_btn(row1, "Remove  (Del)", self.remove_selected)
        edit_btn(row1, "Clear All", self.clear_all, padx=4)
        edit_btn(row1, f"Undo  ({ACCEL}+Z)", self.undo)
        edit_btn(row2, "▲ Up  (Alt+↑)", lambda: self.move_selected(-1))
        edit_btn(row2, "▼ Down  (Alt+↓)", lambda: self.move_selected(1), padx=4)
        edit_btn(row2, "⤒ Top", lambda: self.move_selected_to_end(top=True))
        edit_btn(row2, "⤓ Bottom", lambda: self.move_selected_to_end(top=False), padx=4)
        edit_btn(row2, "Sort A→Z", self.sort_natural)
        edit_btn(row2, "Reverse", self.reverse_order, padx=4)

    def _build_options_panel(self, parent):
        panel = ttk.LabelFrame(parent, text="Layout", padding=6)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.columnconfigure(1, weight=1)
        r = 0

        ttk.Label(panel, text="Preset:").grid(row=r, column=0, sticky="w")
        self.preset_var = tk.StringVar(value="Custom")
        self.preset_combo = ttk.Combobox(panel, textvariable=self.preset_var, state="readonly")
        self.preset_combo.grid(row=r, column=1, sticky="ew", pady=2)
        self.preset_combo.bind("<<ComboboxSelected>>", self._preset_chosen)
        r += 1

        grid_frame = ttk.Frame(panel)
        grid_frame.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(grid_frame, text="Columns (across):").pack(side="left")
        self.cols_var = tk.IntVar(value=1)
        self.cols_spin = ttk.Spinbox(grid_frame, from_=1, to=10000, width=6,
                                     textvariable=self.cols_var,
                                     command=lambda: self._grid_edited("cols"))
        self.cols_spin.pack(side="left", padx=(4, 12))
        self.cols_spin.bind("<KeyRelease>", lambda e: self._grid_edited("cols"))
        ttk.Label(grid_frame, text="Rows (down):").pack(side="left")
        self.rows_var = tk.IntVar(value=1)
        self.rows_spin = ttk.Spinbox(grid_frame, from_=1, to=10000, width=6,
                                     textvariable=self.rows_var,
                                     command=lambda: self._grid_edited("rows"))
        self.rows_spin.pack(side="left", padx=4)
        self.rows_spin.bind("<KeyRelease>", lambda e: self._grid_edited("rows"))
        r += 1

        self.autofit_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(panel, text="Auto-fit the other dimension to the slice count",
                        variable=self.autofit_var,
                        command=lambda: self._grid_edited("cols")).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1

        ttk.Label(panel, text="Fill order:").grid(row=r, column=0, sticky="w")
        self.fill_var = tk.StringVar(value="row")
        fill_frame = ttk.Frame(panel)
        fill_frame.grid(row=r, column=1, sticky="w")
        ttk.Radiobutton(fill_frame, text="Across, then down", value="row",
                        variable=self.fill_var, command=self._update_summary).pack(side="left")
        ttk.Radiobutton(fill_frame, text="Down, then across", value="column",
                        variable=self.fill_var, command=self._update_summary).pack(side="left", padx=6)
        r += 1

        misc = ttk.Frame(panel)
        misc.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(misc, text="Gap (px):").pack(side="left")
        self.gap_var = tk.IntVar(value=2)
        gap_spin = ttk.Spinbox(misc, from_=0, to=100, width=5, textvariable=self.gap_var,
                               command=self._update_summary)
        gap_spin.pack(side="left", padx=(4, 12))
        gap_spin.bind("<KeyRelease>", lambda e: self._update_summary())
        ttk.Label(misc, text="Output scale (%):").pack(side="left")
        self.scale_var = tk.IntVar(value=100)
        scale_spin = ttk.Spinbox(misc, from_=5, to=100, increment=5, width=5,
                                 textvariable=self.scale_var, command=self._update_summary)
        scale_spin.pack(side="left", padx=4)
        scale_spin.bind("<KeyRelease>", lambda e: self._update_summary())
        r += 1

        color_frame = ttk.Frame(panel)
        color_frame.grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(color_frame, text="Background:").pack(side="left")
        self.bg_color = "#000000"
        self.bg_swatch = tk.Label(color_frame, text="   ", bg=self.bg_color, relief="solid", bd=1)
        self.bg_swatch.pack(side="left", padx=4)
        ttk.Button(color_frame, text="Choose…", command=self.choose_bg).pack(side="left")
        r += 1

        self.labels_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(panel, text="Number each slice on the output",
                        variable=self.labels_var).grid(row=r, column=0, columnspan=2, sticky="w", pady=2)
        r += 1

        self.summary_var = tk.StringVar(value="No slices loaded.")
        summary = ttk.Label(panel, textvariable=self.summary_var, wraplength=380, justify="left")
        summary.grid(row=r, column=0, columnspan=2, sticky="w", pady=(8, 2))
        r += 1

        self.warn_var = tk.StringVar(value="")
        warn = ttk.Label(panel, textvariable=self.warn_var, wraplength=380,
                         justify="left", style="Warn.TLabel")
        warn.grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1

        act = ttk.Frame(panel)
        act.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 2))
        self.preview_btn = ttk.Button(act, text=f"Preview  ({ACCEL}+P)", command=self.start_preview)
        self.preview_btn.pack(side="left")
        self.export_btn = ttk.Button(act, text=f"Export…  ({ACCEL}+E)", command=self.start_export)
        self.export_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(act, text="Cancel  (Esc)", command=self.cancel_event.set,
                                     state="disabled")
        self.cancel_btn.pack(side="left")
        self.open_img_btn = ttk.Button(act, text="Open Image", command=self.open_image, state="disabled")
        self.open_img_btn.pack(side="left", padx=6)
        self.open_dir_btn = ttk.Button(act, text="Open Folder", command=self.open_folder, state="disabled")
        self.open_dir_btn.pack(side="left")
        r += 1

        preview_frame = ttk.LabelFrame(panel, text="Preview (click it, then arrow keys scroll)", padding=2)
        preview_frame.grid(row=r, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        panel.rowconfigure(r, weight=1)
        preview_frame.rowconfigure(0, weight=1)
        preview_frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(preview_frame, bg="#404040", highlightthickness=1, takefocus=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        cvsb = ttk.Scrollbar(preview_frame, orient="vertical", command=self.canvas.yview)
        cvsb.grid(row=0, column=1, sticky="ns")
        chsb = ttk.Scrollbar(preview_frame, orient="horizontal", command=self.canvas.xview)
        chsb.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=cvsb.set, xscrollcommand=chsb.set)
        self._bind_canvas_navigation()

    def _bind_canvas_navigation(self):
        c = self.canvas
        c.bind("<Button-1>", lambda e: c.focus_set())
        c.bind("<MouseWheel>", lambda e: c.yview_scroll(_wheel_steps(e), "units"))
        c.bind("<Shift-MouseWheel>", lambda e: c.xview_scroll(_wheel_steps(e), "units"))
        # X11 (Linux) delivers wheel motion as button 4/5 presses instead
        c.bind("<Button-4>", lambda e: c.yview_scroll(-1, "units"))
        c.bind("<Button-5>", lambda e: c.yview_scroll(1, "units"))
        c.bind("<Up>", lambda e: c.yview_scroll(-1, "units"))
        c.bind("<Down>", lambda e: c.yview_scroll(1, "units"))
        c.bind("<Left>", lambda e: c.xview_scroll(-1, "units"))
        c.bind("<Right>", lambda e: c.xview_scroll(1, "units"))
        c.bind("<Prior>", lambda e: c.yview_scroll(-1, "pages"))
        c.bind("<Next>", lambda e: c.yview_scroll(1, "pages"))
        c.bind("<Home>", lambda e: c.yview_moveto(0))
        c.bind("<End>", lambda e: c.yview_moveto(1))

    def _build_bottom_bar(self):
        bar = ttk.Frame(self, padding=(8, 2))
        bar.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(bar, mode="determinate", length=220)
        self.progress.pack(side="right")
        self.status_var = tk.StringVar(value="Add JPEG slices to begin.")
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(side="left", fill="x", expand=True)

    def _bind_keys(self):
        # bind upper- and lowercase keysyms so shortcuts survive Caps Lock;
        # on macOS also bind the Command key (the native convention there)
        modifiers = ("Control", "Command") if MAC else ("Control",)
        for key, action in (("o", self.add_files), ("e", self.start_export),
                            ("p", self.start_preview), ("z", self.undo)):
            for mod in modifiers:
                self.bind(f"<{mod}-{key}>", lambda e, a=action: a())
                self.bind(f"<{mod}-{key.upper()}>", lambda e, a=action: a())
        self.bind("<Escape>", lambda e: self.cancel_event.set() if self.worker else None)
        self.listbox.bind("<Delete>", lambda e: self.remove_selected())
        self.listbox.bind("<BackSpace>", lambda e: self.remove_selected())  # mac keyboards
        for mod in ("Alt",) + (("Command",) if MAC else ()):
            self.bind(f"<{mod}-Up>", lambda e: (self.move_selected(-1), "break")[1])
            self.bind(f"<{mod}-Down>", lambda e: (self.move_selected(1), "break")[1])

    # ------------------------------------------------------- file handling --

    def _busy_blocked(self) -> bool:
        """The slice list is locked while a stitch job runs, so the exported
        order can never silently diverge from the displayed order."""
        if self.worker is not None:
            self.set_status("Busy stitching — the slice list is locked until the job finishes.")
            return True
        return False

    def _push_undo(self):
        self._undo_stack.append(list(self.files))
        del self._undo_stack[:-UNDO_DEPTH]

    def undo(self):
        if self._busy_blocked():
            return
        if not self._undo_stack:
            self.set_status("Nothing to undo.")
            return
        self.files = self._undo_stack.pop()
        self.files = [p for p in self.files if p in self.size_cache or self._recache(p)]
        self._refresh_list()
        self._refresh_presets()
        self.set_status(f"Undid last list change. Total: {len(self.files)}.")

    def _recache(self, p: Path) -> bool:
        try:
            self.size_cache[p] = core.read_sizes([p])[0]
            return True
        except core.ImageReadError:
            return False

    def add_files(self):
        if self._busy_blocked():
            return
        names = filedialog.askopenfilenames(
            title="Choose slice images",
            filetypes=[("Images", "*.jpg *.jpeg *.jfif *.jpe *.png *.bmp *.tif *.tiff *.webp"),
                       ("All files", "*.*")],
        )
        if names:
            self._add_paths([Path(n) for n in names])

    def add_folder(self):
        if self._busy_blocked():
            return
        folder = filedialog.askdirectory(title="Choose a folder of slice images")
        if not folder:
            return
        entries = [p for p in Path(folder).iterdir() if p.is_file()]
        paths = [p for p in entries if p.suffix.lower() in IMAGE_EXTS]
        skipped = len(entries) - len(paths)
        if not paths:
            messagebox.showinfo("No images", "That folder contains no supported image files.")
            return
        self._add_paths(paths, skipped_note=skipped)

    def _add_paths(self, paths: list[Path], skipped_note: int = 0):
        was_empty = not self.files
        self._push_undo()
        paths = sorted(paths, key=lambda p: core.natural_sort_key(p.name))
        bad: list[str] = []
        added = 0
        for p in paths:
            if p in self.files:
                continue
            if not self._recache(p):
                bad.append(p.name)
                continue
            self.files.append(p)
            added += 1
        if not added:
            self._undo_stack.pop()
        if bad:
            shown = "\n".join(bad[:15]) + ("\n…" if len(bad) > 15 else "")
            messagebox.showwarning("Some files skipped",
                                   f"{len(bad)} file(s) could not be read and were skipped:\n{shown}")
        self._refresh_list()
        # only auto-pick a layout on the first add — never clobber a chosen grid
        self._refresh_presets(pick_default=was_empty and added > 0)
        note = f" ({skipped_note} non-image file(s) ignored)" if skipped_note else ""
        self.set_status(f"Added {added} slice(s){note}. Total: {len(self.files)}.")

    def _refresh_list(self, keep_selection: list[int] | None = None):
        self.listbox.delete(0, "end")
        width = len(str(len(self.files)))
        for i, p in enumerate(self.files, 1):
            self.listbox.insert("end", f"{i:>{width}}.  {p.name}")
        if keep_selection:
            visible = [i for i in keep_selection if 0 <= i < len(self.files)]
            for i in visible:
                self.listbox.selection_set(i)
            if visible:
                self.listbox.see(visible[0])
                self.listbox.activate(visible[0])
                self.listbox.selection_anchor(visible[0])
        self._update_summary()

    def _selected(self) -> list[int]:
        return list(self.listbox.curselection())

    def remove_selected(self):
        if self._busy_blocked():
            return
        sel = self._selected()
        if not sel:
            return
        self._push_undo()
        for i in reversed(sel):
            del self.files[i]
        self._refresh_list()
        self._refresh_presets()
        self.set_status(f"Removed {len(sel)} slice(s). Total: {len(self.files)}. Ctrl+Z undoes.")

    def clear_all(self):
        if self._busy_blocked():
            return
        if self.files and not messagebox.askyesno("Clear all", "Remove all slices from the list?"):
            return
        self._push_undo()
        self.files = []
        self._refresh_list()
        self._refresh_presets()
        self.set_status("List cleared. Ctrl+Z undoes.")

    def move_selected(self, delta: int):
        if self._busy_blocked():
            return
        sel = self._selected()
        if not sel:
            return
        self._push_undo()
        order = sel if delta < 0 else list(reversed(sel))
        new_positions = {}
        for i in order:
            j = i + delta
            if j < 0 or j >= len(self.files) or j in new_positions.values():
                new_positions[i] = i
                continue
            self.files[i], self.files[j] = self.files[j], self.files[i]
            new_positions[i] = j
        self._refresh_list(keep_selection=sorted(new_positions.values()))

    def move_selected_to_end(self, *, top: bool):
        if self._busy_blocked():
            return
        sel = self._selected()
        if not sel:
            return
        self._push_undo()
        chosen = [self.files[i] for i in sel]
        rest = [p for i, p in enumerate(self.files) if i not in set(sel)]
        self.files = chosen + rest if top else rest + chosen
        if top:
            keep = list(range(len(chosen)))
        else:
            keep = list(range(len(rest), len(self.files)))
        self._refresh_list(keep_selection=keep)

    def sort_natural(self):
        if self._busy_blocked():
            return
        self._push_undo()
        self.files.sort(key=lambda p: core.natural_sort_key(p.name))
        self._refresh_list()
        self.set_status("Sorted by filename (natural order).")

    def reverse_order(self):
        if self._busy_blocked():
            return
        self._push_undo()
        self.files.reverse()
        self._refresh_list()
        self.set_status("Order reversed.")

    # Drag to reorder: a press only becomes a drag after DRAG_THRESHOLD_PX of
    # vertical movement, and only when exactly one item is selected — so normal
    # clicks and rubber-band multi-selection never silently reorder slices.
    def _drag_start(self, event):
        if self.worker is not None or not self.files:
            self._drag_index = None
            return
        self._drag_index = self.listbox.nearest(event.y)
        self._drag_y = event.y
        self._drag_armed = False

    def _drag_motion(self, event):
        if self._drag_index is None:
            return
        if not self._drag_armed:
            if abs(event.y - self._drag_y) < DRAG_THRESHOLD_PX:
                return
            sel = self._selected()
            if len(sel) != 1 or sel[0] != self._drag_index:
                self._drag_index = None  # multi-select drag: leave it to Tk
                return
            self._drag_armed = True
            self._push_undo()
        target = self.listbox.nearest(event.y)
        if target != self._drag_index and 0 <= target < len(self.files):
            self.files.insert(target, self.files.pop(self._drag_index))
            self._drag_index = target
            self._refresh_list(keep_selection=[target])
        return "break"

    def _drag_end(self, _event):
        if self._drag_armed:
            self.set_status("Slice moved. Ctrl+Z undoes.")
        self._drag_index = None
        self._drag_armed = False

    # ---------------------------------------------------------- grid logic --

    def _refresh_presets(self, pick_default: bool = False):
        n = len(self.files)
        self.presets = core.layout_presets(n)
        values = []
        for c, rws in self.presets:
            tag = "" if c * rws == n else "  (adds blank cells)"
            values.append(f"{c} across × {rws} down{tag}")
        self.preset_combo["values"] = ["Custom"] + values
        if pick_default and self.presets:
            near_square = min(self.presets, key=lambda p: abs(p[0] - p[1]))
            self._set_grid(*near_square)
            self.preset_var.set(self._preset_label(near_square))
        self._update_summary()

    def _preset_label(self, pair: tuple[int, int]) -> str:
        c, rws = pair
        tag = "" if c * rws == len(self.files) else "  (adds blank cells)"
        return f"{c} across × {rws} down{tag}"

    def _preset_chosen(self, _event=None):
        label = self.preset_var.get()
        for pair in self.presets:
            if self._preset_label(pair) == label:
                self._set_grid(*pair)
                return
        self._update_summary()  # "Custom" chosen

    def _set_grid(self, cols: int, rows: int):
        self._syncing_grid = True
        try:
            self.cols_var.set(cols)
            self.rows_var.set(rows)
        finally:
            self._syncing_grid = False
        self._update_summary()

    def _grid_edited(self, which: str):
        if self._syncing_grid:
            return
        n = len(self.files)
        self.preset_var.set("Custom")
        if self.autofit_var.get() and n:
            try:
                given = self.cols_var.get() if which == "cols" else self.rows_var.get()
            except tk.TclError:
                self._update_summary()  # mid-edit: not a number yet — show state honestly
                return
            if given >= 1:
                if which == "cols":
                    _, rows = core.compute_grid(n, cols=given)
                    self._syncing_grid = True
                    self.rows_var.set(rows)
                    self._syncing_grid = False
                else:
                    cols, _ = core.compute_grid(n, rows=given)
                    self._syncing_grid = True
                    self.cols_var.set(cols)
                    self._syncing_grid = False
        self._update_summary()

    def _current_grid(self) -> tuple[int, int] | None:
        try:
            cols, rows = self.cols_var.get(), self.rows_var.get()
        except tk.TclError:
            return None
        if cols < 1 or rows < 1:
            return None
        return cols, rows

    def _current_options(self) -> tuple[int, float] | None:
        """Validated (gap_px, scale) from the spinboxes, or None mid-edit."""
        try:
            gap = self.gap_var.get()
            scale = self.scale_var.get() / 100
        except tk.TclError:
            return None
        if gap < 0 or not (0 < scale <= 1):
            return None
        return gap, scale

    def _set_actions_enabled(self, enabled: bool):
        state = ["!disabled"] if enabled else ["disabled"]
        self.export_btn.state(state)
        self.preview_btn.state(state)

    def _update_summary(self):
        n = len(self.files)
        self.warn_var.set("")
        if not n:
            self.summary_var.set("No slices loaded.")
            self._set_actions_enabled(False)
            return
        grid = self._current_grid()
        if grid is None:
            self.warn_var.set("⚠ Enter valid column and row numbers.")
            self._set_actions_enabled(False)
            return
        opts = self._current_options()
        if opts is None:
            self.warn_var.set("⚠ Enter a gap of 0 or more and an output scale between 5 and 100.")
            self._set_actions_enabled(False)
            return
        cols, rows = grid
        gap, scale = opts
        if cols * rows < n:
            self.warn_var.set(
                f"⚠ A {cols} × {rows} grid holds only {cols * rows} slices — you have {n}. "
                "Increase columns or rows (or turn Auto-fit on).")
            self._set_actions_enabled(False)
            return
        sizes = [self.size_cache[p] for p in self.files]
        w, h = core.output_size(sizes, cols=cols, rows=rows, gap=gap, scale=scale)
        note = ""
        if w > core.JPEG_MAX_DIM or h > core.JPEG_MAX_DIM:
            note = "  — too large for JPEG, export as PNG/TIFF"
        blanks = cols * rows - n
        blank_note = f", {blanks} blank cell(s)" if blanks else ""
        self.summary_var.set(
            f"{n} slices → {cols} across × {rows} down{blank_note} → {w:,} × {h:,} px{note}")
        if self.worker is None:
            self._set_actions_enabled(True)

    def choose_bg(self):
        color = colorchooser.askcolor(initialcolor=self.bg_color, title="Background color")
        if color and color[1]:
            self.bg_color = color[1]
            self.bg_swatch.configure(bg=self.bg_color)

    # ------------------------------------------------------ preview/export --

    def _stitch_kwargs(self, *, scale: float, gap: int) -> dict:
        cols, rows = self._current_grid()
        return dict(
            cols=cols, rows=rows,
            fill=self.fill_var.get(),
            gap=gap,
            bg=self.bg_color,
            scale=scale,
            labels=self.labels_var.get(),
            progress=lambda i, n: self.msgq.put(("progress", i, n)),
            cancel=self.cancel_event,
        )

    def start_preview(self):
        if self.worker or not self._ready():
            return
        gap, scale = self._current_options()
        cols, rows = self._current_grid()
        sizes = [self.size_cache[p] for p in self.files]
        w, h = core.output_size(sizes, cols=cols, rows=rows, gap=gap, scale=scale)
        shrink = min(1.0, PREVIEW_MAX_SIDE / max(w, h))
        kwargs = self._stitch_kwargs(scale=scale * shrink, gap=max(0, round(gap * shrink)))
        self._start_worker("preview", kwargs)

    def start_export(self):
        if self.worker or not self._ready():
            return
        gap, scale = self._current_options()
        out = filedialog.asksaveasfilename(
            title="Save stitched ribbon",
            defaultextension=".png",
            initialfile="mri_ribbon.png",
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg"), ("TIFF image", "*.tiff")],
        )
        if not out:
            return
        # exported dimensions match the summary exactly: same gap, same scale
        kwargs = self._stitch_kwargs(scale=scale, gap=gap)
        self._start_worker("export", kwargs, out_path=Path(out))

    def _ready(self) -> bool:
        if not self.files:
            self.set_status("Add slices first.")
            return False
        if self._current_grid() is None or self._current_options() is None or self.warn_var.get():
            self.set_status("Fix the highlighted layout problem first.")
            return False
        return True

    def _start_worker(self, kind: str, kwargs: dict, out_path: Path | None = None):
        self.cancel_event.clear()
        self.progress.configure(maximum=len(self.files), value=0)
        self._set_busy(True)
        self.set_status("Stitching…")
        paths = list(self.files)

        def run():
            try:
                img = core.stitch(paths, **kwargs)
                if kind == "export":
                    core.save_image(img, out_path)
                    self.msgq.put(("done", "export", out_path))
                else:
                    self.msgq.put(("done", "preview", img))
            except core.StitchCancelled:
                self.msgq.put(("error", "Cancelled."))
            except MemoryError:
                self.msgq.put(("error",
                               "Out of memory building the image. Lower the output scale % and retry."))
            except core.StitchError as exc:
                self.msgq.put(("error", str(exc)))
            except Exception as exc:  # surface anything unexpected rather than dying silently
                self.msgq.put(("error", f"Unexpected error: {exc}"))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _set_busy(self, busy: bool):
        self.cancel_btn.state(["!disabled"] if busy else ["disabled"])
        for b in self._edit_buttons:
            b.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self._set_actions_enabled(False)
        else:
            self.progress.configure(value=0)
            self._update_summary()  # decides whether export/preview re-enable

    def _poll_queue(self):
        try:
            while True:
                msg = self.msgq.get_nowait()
                if msg[0] == "progress":
                    _, i, n = msg
                    self.progress.configure(maximum=n, value=i)
                    self.set_status(f"Stitching slice {i} of {n}…")
                elif msg[0] == "done":
                    self.worker = None
                    self._set_busy(False)
                    if msg[1] == "export":
                        self.last_export = msg[2]
                        self.open_img_btn.state(["!disabled"])
                        self.open_dir_btn.state(["!disabled"])
                        self.set_status(f"Saved: {msg[2]}")
                        messagebox.showinfo("Export complete", f"Ribbon saved to:\n{msg[2]}")
                    else:
                        self._show_preview(msg[2])
                        self.set_status("Preview ready (scaled down; export is full quality).")
                elif msg[0] == "error":
                    self.worker = None
                    self._set_busy(False)
                    self.set_status(msg[1])
                    if msg[1] != "Cancelled.":
                        messagebox.showerror("Stitch failed", msg[1])
        except queue.Empty:
            pass
        except Exception as exc:  # a bad message must never kill the poller
            self.worker = None
            self._set_busy(False)
            self.set_status(f"Internal error: {exc}")
        finally:
            self.after(60, self._poll_queue)

    def _show_preview(self, img: Image.Image):
        self.preview_photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.preview_photo)
        self.canvas.configure(scrollregion=(0, 0, img.width, img.height))

    def _on_close(self):
        if self.worker is not None:
            if not messagebox.askyesno(
                    "Job still running",
                    "A stitch job is still running. Cancel it and quit?\n"
                    "(No partially written file will be left behind.)"):
                return
            self.cancel_event.set()
            self.worker.join(timeout=3)
        self.destroy()

    def open_image(self):
        self._open_path(self.last_export)

    def open_folder(self):
        self._open_path(self.last_export.parent if self.last_export else None)

    def _open_path(self, path: Path | None):
        if path is None or not path.exists():
            self.set_status("The exported file is gone (moved or deleted). Export again.")
            self.open_img_btn.state(["disabled"])
            self.open_dir_btn.state(["disabled"])
            return
        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif MAC:
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except OSError as exc:
            self.set_status(f"The system could not open {path.name}: {exc}")

    def set_status(self, text: str):
        self.status_var.set(text)


def _selftest() -> int:
    """Headless sanity check used to verify packaged builds (exit 0 = OK)."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for i in range(6):
            p = Path(d) / f"slice{i + 1}.png"
            Image.new("RGB", (32, 32), (40 * i, 10, 10)).save(p)
            paths.append(p)
        img = core.stitch(paths, cols=3, rows=2, gap=2, labels=True)
        if img.size != (3 * 32 + 2 * 2, 2 * 32 + 2):
            return 1
        out = core.save_image(img, Path(d) / "out.png")
        with Image.open(out) as back:
            if back.size != img.size:
                return 1
    return 0


def main():
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    app = RibbonApp()
    app.mainloop()


if __name__ == "__main__":
    main()
