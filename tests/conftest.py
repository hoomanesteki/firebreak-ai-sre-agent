"""Test setup shared by every suite.

The hygiene script and the commit message hook live in scripts/, which is
not an installed package, so their directories go on the import path here
rather than in each test module.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

for extra_path in (REPO_ROOT / "scripts", REPO_ROOT / "scripts" / "hooks"):
    if str(extra_path) not in sys.path:
        sys.path.insert(0, str(extra_path))
