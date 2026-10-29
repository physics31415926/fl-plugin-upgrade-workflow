"""Stable application resource paths, independent of the working directory."""
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = APP_ROOT / "web" / "templates"
ASSETS = APP_ROOT / "web" / "assets"
EXAMPLES = APP_ROOT / "examples"
