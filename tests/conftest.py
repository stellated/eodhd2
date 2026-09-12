import sys
from pathlib import Path

# src/tips_io.py does `from eodhd_io import ...` (a bare, non-package import),
# which only resolves if src/ itself is on sys.path -- true when running a
# script from inside src/, but not when pytest imports it as `src.tips_io`.
# Add src/ to sys.path so both import styles work under pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
