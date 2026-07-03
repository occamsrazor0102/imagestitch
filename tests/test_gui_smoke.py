"""GUI smoke test — builds the app, drives non-dialog handlers, tears down.

Skipped automatically when no display is available.
"""

import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image


class DialogStub:
    """Replaces tkinter.messagebox so tests can never block on a modal dialog."""

    def __init__(self):
        self.calls = []

    def _record(self, kind, *args, **kwargs):
        self.calls.append((kind, args))
        return True

    def showinfo(self, *a, **k):
        return self._record("showinfo", *a)

    def showwarning(self, *a, **k):
        return self._record("showwarning", *a)

    def showerror(self, *a, **k):
        return self._record("showerror", *a)

    def askyesno(self, *a, **k):
        self._record("askyesno", *a)
        return True


def _display_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


class TestWheelSteps(unittest.TestCase):
    """Pure function — no display needed."""

    def test_windows_and_mac_deltas(self):
        import mri_ribbon_stitcher as gui

        class E:
            def __init__(self, delta):
                self.delta = delta

        with mock.patch.object(gui, "MAC", False):
            self.assertEqual(gui._wheel_steps(E(120)), -1)   # wheel up -> scroll up
            self.assertEqual(gui._wheel_steps(E(-240)), 2)
        with mock.patch.object(gui, "MAC", True):
            self.assertEqual(gui._wheel_steps(E(3)), -3)     # small mac deltas must not floor to 0
            self.assertEqual(gui._wheel_steps(E(-1)), 1)


@unittest.skipUnless(_display_available(), "no display available")
class TestGuiSmoke(unittest.TestCase):
    def setUp(self):
        import mri_ribbon_stitcher
        self.dialogs = DialogStub()
        self.patcher = mock.patch.object(mri_ribbon_stitcher, "messagebox", self.dialogs)
        self.patcher.start()
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.paths = []
        for i in range(12):
            p = d / f"slice{i + 1:02d}.jpg"
            Image.new("RGB", (32, 32), (i * 20, 0, 0)).save(p)
            self.paths.append(p)
        self.app = mri_ribbon_stitcher.RibbonApp()
        self.app.withdraw()

    def tearDown(self):
        self.patcher.stop()
        self.app.destroy()
        self.tmp.cleanup()

    def _pump_until_idle(self, timeout=30):
        """Run the Tk event loop until the worker finishes and is reaped."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.update()
            if self.app.worker is None:
                return
            time.sleep(0.02)
        self.fail("worker did not finish in time")

    def test_add_reorder_layout_export(self):
        app = self.app
        app._add_paths(self.paths)
        self.assertEqual(len(app.files), 12)
        self.assertEqual(app.listbox.size(), 12)

        # duplicates are ignored
        app._add_paths(self.paths[:3])
        self.assertEqual(len(app.files), 12)

        # near-square default picked: 3 across x 4 down for 12
        self.assertEqual(app._current_grid(), (3, 4))
        self.assertIn("12 slices", app.summary_var.get())

        # reorder: move first slice down one, then reverse, then re-sort
        first = app.files[0]
        app.listbox.selection_set(0)
        app.move_selected(1)
        self.assertEqual(app.files[1], first)
        app.reverse_order()
        self.assertEqual(app.files[-2], first)
        app.sort_natural()
        self.assertEqual(app.files[0], first)

        # auto-fit: setting cols recomputes rows
        app.cols_var.set(5)
        app._grid_edited("cols")
        self.assertEqual(app._current_grid(), (5, 3))
        self.assertIn("blank cell", app.summary_var.get())

        # invalid grid disables export
        app.autofit_var.set(False)
        app.cols_var.set(2)
        app.rows_var.set(2)
        app._grid_edited("cols")
        self.assertTrue(app.warn_var.get())
        self.assertIn("disabled", app.export_btn.state())

        # valid again
        app.autofit_var.set(True)
        app.cols_var.set(4)
        app._grid_edited("cols")
        self.assertFalse(app.warn_var.get())

        # export through the worker path (bypassing the save dialog)
        out = Path(self.tmp.name) / "ribbon.png"
        kwargs = app._stitch_kwargs(scale=1.0, gap=app.gap_var.get())
        app._start_worker("export", kwargs, out_path=out)
        self._pump_until_idle()
        self.assertTrue(out.exists())
        self.assertIn("showinfo", [c[0] for c in self.dialogs.calls])
        self.assertEqual(app.last_export, out)
        with Image.open(out) as img:
            self.assertEqual(img.size, (4 * 32 + 3 * 2, 3 * 32 + 2 * 2))

    def test_undo_and_busy_lock(self):
        import threading
        app = self.app
        app._add_paths(self.paths)
        first = app.files[0]

        # undo restores the order after a move
        app.listbox.selection_set(0)
        app.move_selected(1)
        self.assertEqual(app.files[1], first)
        app.undo()
        self.assertEqual(app.files[0], first)

        # while a worker "runs", every order-mutating action is refused
        app.worker = threading.current_thread()  # simulate a running job
        before = list(app.files)
        app.listbox.selection_set(0)
        app.move_selected(1)
        app.reverse_order()
        app.remove_selected()
        self.assertEqual(app.files, before)
        self.assertIn("locked", app.status_var.get())
        app.worker = None

    def test_adding_more_files_keeps_custom_grid(self):
        app = self.app
        app._add_paths(self.paths[:9])          # first add: near-square 3x3 picked
        self.assertEqual(app._current_grid(), (3, 3))
        app.autofit_var.set(False)
        app.cols_var.set(2)
        app.rows_var.set(5)
        app._grid_edited("cols")
        self.assertEqual(app._current_grid(), (2, 5))
        app._add_paths(self.paths[9:])           # second add must NOT clobber it
        self.assertEqual(app._current_grid(), (2, 5))
        self.assertTrue(app.warn_var.get())      # 2x5 < 12 now, warned honestly

    def test_remove_and_clear(self):
        app = self.app
        app._add_paths(self.paths[:5])
        app.listbox.selection_set(1, 2)
        app.remove_selected()
        self.assertEqual(len(app.files), 3)
        app.files.clear()
        app.size_cache.clear()
        app._refresh_list()
        self.assertEqual(app.summary_var.get(), "No slices loaded.")
        self.assertIn("disabled", app.export_btn.state())


if __name__ == "__main__":
    unittest.main()
