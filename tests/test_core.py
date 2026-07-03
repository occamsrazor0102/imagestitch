import math
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image

import stitcher_core as core


def make_image(path: Path, size, color):
    Image.new("RGB", size, color).save(path)
    return path


class TestNaturalSort(unittest.TestCase):
    def test_numeric_aware(self):
        names = ["slice10.jpg", "slice2.jpg", "slice1.jpg"]
        out = sorted(names, key=core.natural_sort_key)
        self.assertEqual(out, ["slice1.jpg", "slice2.jpg", "slice10.jpg"])

    def test_case_insensitive(self):
        out = sorted(["B.jpg", "a.jpg"], key=core.natural_sort_key)
        self.assertEqual(out, ["a.jpg", "B.jpg"])

    def test_multi_number_segments(self):
        names = ["s1_p10.jpg", "s1_p2.jpg", "s1_p1.jpg"]
        out = sorted(names, key=core.natural_sort_key)
        self.assertEqual(out, ["s1_p1.jpg", "s1_p2.jpg", "s1_p10.jpg"])

    def test_unicode_digitlike_never_crashes(self):
        # '²' and '①' are isdigit() but int() rejects them; '٢' is a real
        # decimal digit. None of these may raise.
        for name in ["①1.jpg", "scan2²3.jpg", "slice٢.jpg", "²²².jpg"]:
            core.natural_sort_key(name)
        out = sorted(["scan2²3.jpg", "scan2.jpg"], key=core.natural_sort_key)
        self.assertEqual(out[0], "scan2.jpg")

    def test_mixed_alpha_numeric_segments_comparable(self):
        # keys must be mutually comparable whatever mix of text/number segments
        names = ["1a.jpg", "a1.jpg", "²x.jpg", "10.jpg", "x.jpg"]
        sorted(names, key=core.natural_sort_key)  # must not raise TypeError


class TestPresetsAndGrid(unittest.TestCase):
    def test_presets_200(self):
        pairs = core.layout_presets(200)
        for expected in [(1, 200), (2, 100), (4, 50), (5, 40), (8, 25), (10, 20),
                         (20, 10), (25, 8), (40, 5), (50, 4), (100, 2), (200, 1)]:
            self.assertIn(expected, pairs)
        self.assertIn((15, 14), pairs)  # near-square with blanks
        for c, r in pairs:
            self.assertGreaterEqual(c * r, 200)

    def test_presets_prime(self):
        pairs = core.layout_presets(7)
        self.assertIn((1, 7), pairs)
        self.assertIn((7, 1), pairs)
        self.assertIn((3, 3), pairs)

    def test_presets_empty(self):
        self.assertEqual(core.layout_presets(0), [])

    def test_compute_grid(self):
        self.assertEqual(core.compute_grid(200, cols=10), (10, 20))
        self.assertEqual(core.compute_grid(200, rows=3), (67, 3))
        self.assertEqual(core.compute_grid(1, cols=5), (5, 1))
        with self.assertRaises(ValueError):
            core.compute_grid(10)
        with self.assertRaises(ValueError):
            core.compute_grid(10, cols=2, rows=5)
        with self.assertRaises(ValueError):
            core.compute_grid(0, cols=1)


class TestStitch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.red = make_image(d / "a.png", (10, 8), (255, 0, 0))
        cls.green = make_image(d / "b.png", (10, 8), (0, 255, 0))
        cls.blue = make_image(d / "c.png", (10, 8), (0, 0, 255))
        cls.small = make_image(d / "d.png", (4, 4), (255, 255, 0))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_row_fill_geometry(self):
        img = core.stitch([self.red, self.green, self.blue],
                          cols=2, rows=2, gap=2, bg="#000000")
        self.assertEqual(img.size, (2 * 10 + 2, 2 * 8 + 2))
        self.assertEqual(img.getpixel((5, 4)), (255, 0, 0))      # cell (0,0)
        self.assertEqual(img.getpixel((17, 4)), (0, 255, 0))     # cell (0,1)
        self.assertEqual(img.getpixel((5, 14)), (0, 0, 255))     # cell (1,0)
        self.assertEqual(img.getpixel((17, 14)), (0, 0, 0))      # blank cell
        self.assertEqual(img.getpixel((11, 4)), (0, 0, 0))       # gap column

    def test_column_fill_geometry(self):
        img = core.stitch([self.red, self.green, self.blue],
                          cols=2, rows=2, fill="column", bg="#ffffff")
        self.assertEqual(img.getpixel((5, 4)), (255, 0, 0))      # (0,0) first
        self.assertEqual(img.getpixel((5, 12)), (0, 255, 0))     # (1,0) second: down first
        self.assertEqual(img.getpixel((15, 4)), (0, 0, 255))     # (0,1) third
        self.assertEqual(img.getpixel((15, 12)), (255, 255, 255))

    def test_mixed_sizes_centered(self):
        img = core.stitch([self.red, self.small], cols=2, rows=1, bg="#000000")
        self.assertEqual(img.size, (20, 8))
        # small 4x4 centered in a 10x8 cell at x offset 10: paste at (13, 2)
        self.assertEqual(img.getpixel((15, 4)), (255, 255, 0))
        self.assertEqual(img.getpixel((11, 4)), (0, 0, 0))

    def test_scale(self):
        img = core.stitch([self.red], cols=1, rows=1, scale=0.5)
        self.assertEqual(img.size, (5, 4))

    def test_progress_and_order(self):
        seen = []
        core.stitch([self.red, self.green], cols=2, rows=1,
                    progress=lambda i, n: seen.append((i, n)))
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_cancel(self):
        ev = threading.Event()
        ev.set()
        with self.assertRaises(core.StitchCancelled):
            core.stitch([self.red], cols=1, rows=1, cancel=ev)

    def test_grid_too_small(self):
        with self.assertRaises(ValueError):
            core.stitch([self.red, self.green, self.blue], cols=1, rows=2)

    def test_bad_fill(self):
        with self.assertRaises(ValueError):
            core.stitch([self.red], cols=1, rows=1, fill="diagonal")

    def test_unreadable_file(self):
        bogus = Path(self.tmp.name) / "not_an_image.jpg"
        bogus.write_text("hello")
        with self.assertRaises(core.ImageReadError):
            core.stitch([bogus], cols=1, rows=1)

    def test_labels_change_pixels(self):
        plain = core.stitch([self.red], cols=1, rows=1)
        labelled = core.stitch([self.red], cols=1, rows=1, labels=True)
        self.assertEqual(plain.size, labelled.size)
        self.assertNotEqual(plain.tobytes(), labelled.tobytes())

    def test_output_size_helper(self):
        sizes = [(512, 512)] * 200
        w, h = core.output_size(sizes, cols=10, rows=20, gap=4)
        self.assertEqual((w, h), (10 * 512 + 9 * 4, 20 * 512 + 19 * 4))


class TestSave(unittest.TestCase):
    def test_jpeg_too_big(self):
        img = Image.new("RGB", (core.JPEG_MAX_DIM + 1, 1))
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(core.JpegSizeError):
                core.save_image(img, Path(d) / "big.jpg")

    def test_round_trip_png(self):
        import tempfile
        img = Image.new("RGB", (30, 20), (1, 2, 3))
        with tempfile.TemporaryDirectory() as d:
            out = core.save_image(img, Path(d) / "x.png")
            with Image.open(out) as back:
                self.assertEqual(back.size, (30, 20))
            # atomic write: no .part temp file left behind
            self.assertEqual([p.name for p in Path(d).iterdir()], ["x.png"])

    def test_unknown_extension(self):
        img = Image.new("RGB", (2, 2))
        with self.assertRaises(ValueError):
            core.save_image(img, "out.webp")


class TestIntegration200(unittest.TestCase):
    def test_full_ribbon(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            paths = []
            colors = []
            for i in range(200):
                p = Path(d) / f"slice{i + 1:03d}.png"  # PNG: exact pixel checks
                color = (i, 255 - i, 40)
                Image.new("RGB", (64, 64), color).save(p)
                paths.append(p)
                colors.append(color)

            # every slice must land in exactly the right cell, in order
            ribbon = core.stitch(paths, cols=1, rows=200)
            self.assertEqual(ribbon.size, (64, 200 * 64))
            for i in range(200):
                self.assertEqual(ribbon.getpixel((32, i * 64 + 32)), colors[i],
                                 f"1x200 ribbon: slice {i + 1} misplaced")

            grid = core.stitch(paths, cols=10, rows=20, gap=2)
            self.assertEqual(grid.size, (10 * 64 + 9 * 2, 20 * 64 + 19 * 2))
            for i in range(200):
                r, c = divmod(i, 10)
                x, y = c * 66 + 32, r * 66 + 32
                self.assertEqual(grid.getpixel((x, y)), colors[i],
                                 f"10x20 row-fill: slice {i + 1} misplaced")

            colgrid = core.stitch(paths, cols=10, rows=20, fill="column")
            for i in range(200):
                c, r = divmod(i, 20)
                self.assertEqual(colgrid.getpixel((c * 64 + 32, r * 64 + 32)), colors[i],
                                 f"10x20 column-fill: slice {i + 1} misplaced")

            near_square = core.stitch(paths, cols=15, rows=14, labels=True)
            self.assertEqual(near_square.size, (15 * 64, 14 * 64))


if __name__ == "__main__":
    unittest.main()
