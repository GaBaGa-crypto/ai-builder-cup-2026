"""inspect_source: give the LLM a compact, factual view of an unknown dataset folder."""
from __future__ import annotations

import collections
import csv
import os
from pathlib import Path

import soundfile as sf

from ._paths import redact_data_root, resolve_under_data_root

AUDIO_EXT = {".wav", ".flac", ".ogg", ".aiff", ".aif"}
TABLE_EXT = {".csv", ".tsv"}
TEXT_NAMES = ("readme", "description", "license", "about")
TEXT_EXT = {".md", ".txt", ".rst"}
MAX_SCAN_FILES = 20000
MAX_SCAN_FOLDERS = 2000
MAX_TEXT_FILES = 16


def _bounded(value: int, default: int, upper: int) -> int:
    try:
        return max(0, min(int(value), upper))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_file(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return not path.is_symlink() and path.is_file()
    except (OSError, ValueError):
        return False


def inspect_source(source_dir: str, max_audio: int = 12, max_tables: int = 8, max_text_chars: int = 4000) -> dict:
    """Summarize a dataset folder: directory tree with file counts, audio formats, table headers and README text.

    Args:
        source_dir: Folder that holds the external dataset.
        max_audio: How many audio files to open for format info (spread across folders).
        max_tables: How many CSV files to read headers and first rows from.
        max_text_chars: Max characters returned per README/description file.

    Returns:
        dict with keys: root, n_files, by_extension, folders (relative path -> file count and examples),
        audio (format per sampled file), tables (header + 3 rows), texts (README content).
    """
    max_audio = _bounded(max_audio, 12, 32)
    max_tables = _bounded(max_tables, 8, 16)
    max_text_chars = _bounded(max_text_chars, 4000, 12000)
    try:
        root = resolve_under_data_root(source_dir)
    except ValueError as e:
        return {"error": str(e), "root": Path(source_dir).name or "path"}
    if not root.is_dir():
        shown = redact_data_root(root) if os.environ.get("NORMALIZER_DATA_ROOT") else source_dir
        return {"error": f"not a directory: {shown}"}
    by_ext = collections.Counter()
    folders = collections.defaultdict(list)
    scan_truncated = False
    n_seen = 0
    for folder_no, (dirpath, dirnames, files) in enumerate(os.walk(root), 1):
        if folder_no > MAX_SCAN_FOLDERS or n_seen >= MAX_SCAN_FILES:
            scan_truncated = True
            dirnames[:] = []
            break
        dirnames[:] = sorted(d for d in dirnames
                             if not d.startswith(("_", ".")) and not (Path(dirpath) / d).is_symlink())
        files = [f for f in files if not f.startswith(("_", ".")) and _safe_file(root, Path(dirpath) / f)]
        if n_seen + len(files) > MAX_SCAN_FILES:
            files = files[:MAX_SCAN_FILES - n_seen]
            dirnames[:] = []
            scan_truncated = True
        n_seen += len(files)
        rel = Path(dirpath).relative_to(root).as_posix() or "."
        for f in sorted(files):
            by_ext[Path(f).suffix.lower()] += 1
            folders[rel].append(f)

    folder_view = {k: {"n_files": len(v), "examples": v[:3]} for k, v in sorted(folders.items())}

    audio, tables, texts = [], [], []
    # spread audio sampling across folders
    audio_paths = []
    for rel, files in sorted(folders.items()):
        for f in files:
            if Path(f).suffix.lower() in AUDIO_EXT:
                audio_paths.append(root / rel / f)
                break
    for rel, files in sorted(folders.items()):
        for f in files[1:3]:
            if Path(f).suffix.lower() in AUDIO_EXT:
                audio_paths.append(root / rel / f)
    for p in audio_paths[:max_audio]:
        try:
            i = sf.info(str(p))
            audio.append({"file": p.relative_to(root).as_posix(), "sample_rate": i.samplerate, "channels": i.channels,
                          "subtype": i.subtype, "duration_s": round(i.duration, 3)})
        except Exception as e:  # noqa: BLE001
            audio.append({"file": p.relative_to(root).as_posix(), "error": str(e)})

    n_tab = 0
    for rel, files in sorted(folders.items()):
        for f in files:
            if n_tab >= max_tables:
                break
            if Path(f).suffix.lower() in TABLE_EXT:
                p = root / rel / f
                try:
                    with open(p, encoding="utf-8-sig", errors="replace") as fh:
                        rows = [[cell[:500] for cell in row[:50]] for _, row in zip(range(4), csv.reader(fh))]
                    tables.append({"file": p.relative_to(root).as_posix(), "header": rows[0] if rows else [],
                                   "rows": rows[1:4]})
                    n_tab += 1
                except Exception as e:  # noqa: BLE001
                    tables.append({"file": p.relative_to(root).as_posix(), "error": str(e)})

    for rel, files in sorted(folders.items()):
        for f in files:
            if len(texts) >= MAX_TEXT_FILES:
                break
            low = f.lower()
            if Path(f).suffix.lower() in TEXT_EXT or any(low.startswith(n) for n in TEXT_NAMES):
                p = root / rel / f
                try:
                    texts.append({"file": p.relative_to(root).as_posix(),
                                  "text": p.read_text(encoding="utf-8", errors="replace")[:max_text_chars]})
                except Exception as e:  # noqa: BLE001
                    texts.append({"file": p.relative_to(root).as_posix(), "error": str(e)})

    return {"root": str(root), "n_files": sum(by_ext.values()), "scan_truncated": scan_truncated,
            "by_extension": dict(by_ext),
            "folders": folder_view, "audio": audio, "tables": tables, "texts": texts}
