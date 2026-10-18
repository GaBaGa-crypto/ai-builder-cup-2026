"""kaggle_dataset_info: read a public Kaggle dataset's description, license and file list (no credentials, read-only).

Kaggle downloads do not include the dataset page text, but that text is often the only place where machine, tool,
workpiece and label meanings are written down.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

API = "https://www.kaggle.com/api/v1"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REF_CHARS = 256


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "cutsound-normalizer"})
    with urllib.request.urlopen(req, timeout=30) as r:
        final = urllib.parse.urlsplit(r.geturl())
        if final.scheme != "https" or final.hostname != "www.kaggle.com":
            raise ValueError("Kaggle request redirected outside the allowed host")
        raw = r.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Kaggle response exceeded the 2 MiB limit")
        return json.loads(raw.decode("utf-8"))


def kaggle_dataset_info(dataset_ref: str, max_description_chars: int = 6000, max_files: int = 200) -> dict:
    """Fetch the public description, license, citation hints and file list of a Kaggle dataset.

    Args:
        dataset_ref: 'owner/slug' or a full kaggle.com/datasets/... URL.
        max_description_chars: Truncate the description to this many characters.
        max_files: Maximum number of file names to return.

    Returns:
        dict with title, subtitle, license, url, total_bytes, description, files (name, bytes), or an error.
    """
    try:
        max_description_chars = max(0, min(int(max_description_chars), 12000))
        max_files = max(0, min(int(max_files), 200))
    except (TypeError, ValueError, OverflowError):
        return {"error": "max_description_chars and max_files must be integers"}
    if not isinstance(dataset_ref, str):
        return {"error": "dataset_ref must be a string"}
    ref = dataset_ref.strip()
    if len(ref) > MAX_REF_CHARS:
        return {"error": "dataset ref is too long"}
    if "://" in ref:
        parsed = urllib.parse.urlsplit(ref)
        if parsed.scheme != "https" or parsed.hostname not in ("kaggle.com", "www.kaggle.com"):
            return {"error": "full dataset URLs must use https://www.kaggle.com/datasets/..."}
        parts = parsed.path.strip("/").split("/")
        ref = "/".join(parts[1:3]) if len(parts) >= 3 and parts[0] == "datasets" else ""
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", ref)
    if not m:
        return {"error": f"cannot parse dataset ref {dataset_ref!r}; expected owner/slug"}
    owner, slug = m.group(1), m.group(2)
    try:
        d = _get(f"{API}/datasets/view/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(slug, safe='')}")
    except Exception as e:  # noqa: BLE001
        return {"error": f"Kaggle metadata unavailable: {type(e).__name__}: {e}"}
    files = []
    try:
        fl = _get(f"{API}/datasets/list/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(slug, safe='')}?pageSize={max_files}")
        files = [{"name": f.get("name"), "bytes": f.get("totalBytes")} for f in (fl.get("datasetFiles") or [])][:max_files]
    except Exception:  # noqa: BLE001  file list is optional
        pass
    desc = d.get("description") or ""
    return {"ref": f"{owner}/{slug}", "title": d.get("title"), "subtitle": d.get("subtitle"),
            "license": d.get("licenseName"), "url": d.get("url") or f"https://www.kaggle.com/datasets/{owner}/{slug}",
            "creator": d.get("creatorName"), "total_bytes": d.get("totalBytes"),
            "version": d.get("currentVersionNumber"), "description": desc[:max_description_chars],
            "description_truncated": len(desc) > max_description_chars, "files": files}
