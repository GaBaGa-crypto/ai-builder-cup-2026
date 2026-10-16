"""Load the Gemini API key from a file the user points to, without ever logging or storing it elsewhere.

Order: GOOGLE_API_KEY already in the environment wins; otherwise GOOGLE_API_KEY_FILE names a text file whose first
non-empty line is the key. Nothing is printed and nothing is written. Enterprise (GCP) auth needs none of this.
"""
from __future__ import annotations

import os
from pathlib import Path


def load_api_key_from_file() -> str:
    """Populate GOOGLE_API_KEY from GOOGLE_API_KEY_FILE if needed. Returns 'env', 'file' or 'none'."""
    if os.environ.get("GOOGLE_API_KEY"):
        return "env"
    p = os.environ.get("GOOGLE_API_KEY_FILE")
    if not p:
        return "none"
    path = Path(p)
    if not path.is_file():
        raise FileNotFoundError(f"GOOGLE_API_KEY_FILE points to a missing file: {path}")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        if "=" in key:  # accept 'NAME = value' / 'NAME=value' lines as well as a bare key
            key = key.split("=", 1)[1].strip()
        key = key.strip("\"' ")
        if key:
            os.environ["GOOGLE_API_KEY"] = key
            return "file"
    raise ValueError(f"GOOGLE_API_KEY_FILE has no key line: {path}")
