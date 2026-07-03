"""Double-click launcher — runs the GUI without a console window."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mri_ribbon_stitcher import main

main()
