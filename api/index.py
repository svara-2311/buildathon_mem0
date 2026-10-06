"""Vercel entrypoint: every request is routed here by vercel.json and handled by the FastAPI app."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import app  # noqa: E402  (path setup must run first)
