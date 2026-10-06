"""Deployment entrypoint. Vercel's FastAPI detection looks for an ASGI `app` in main.py at the repo root
and routes every path to it, so no vercel.json rewrite is needed (a rewrite would replace the request
path with the destination and break routing). Locally, `uvicorn server:app` or `uvicorn main:app` both work.
"""
from server import app

__all__ = ["app"]
