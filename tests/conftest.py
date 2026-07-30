"""Shared pytest configuration.

Ensures the repo root is on ``sys.path`` so ``sentinel.*`` and ``hooks.*``
import cleanly when pytest is run without the project installed.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
