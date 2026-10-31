"""Pytest bootstrap: put src/ on sys.path so tests import shadowfenced."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
