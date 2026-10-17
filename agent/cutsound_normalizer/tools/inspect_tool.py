"""inspect_source: give the LLM a compact, factual view of an unknown dataset folder."""
from __future__ import annotations

import collections
import csv
import os
from pathlib import Path

import soundfile as sf

AUDIO_EXT = {".wav", ".flac", ".ogg", ".aiff", ".aif"}
TABLE_EXT = {".csv", ".tsv"}
TEXT_NAMES = ("readme", "description", "license", "about")
TEXT_EXT = {".md", ".txt", ".rst"}


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
    root = Path(source_dir)
    # Deployed instances set NORMALIZER_DATA_ROOT (e.g. /data); folders outside it are refused so an anonymous
    # caller cannot enumerate the container (finding from the 2026-10-17 cross-engine audit).
    data_root = os.environ.get("NORMALIZER_DATA_ROOT")
    if data_root:
        try:
            root.resolve().relative_to(Path(data_root).resolve())
        except ValueError:
            return {"error": f"source_dir must be inside the data root {data_root!r}", "root": str(source_dir)}
    if not root.is_dir():
        return {"error": f"not a directory: {source_dir}"}
    by_ext = collections.Counter()
    folders = collections.defaultdict(list)
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(("_", ".")))  # not part of the dataset
        files = [f for f in files if not f.startswith(("_", "."))]
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
                        rows = [r for _, r in zip(range(4), csv.reader(fh))]
                    tables.append({"file": p.relative_to(root).as_posix(), "header": rows[0] if rows else [],
                                   "rows": rows[1:4]})
                    n_tab += 1
                except Exception as e:  # noqa: BLE001
                    tables.append({"file": p.relative_to(root).as_posix(), "error": str(e)})

    for rel, files in sorted(folders.items()):
        for f in files:
            low = f.lower()
            if Path(f).suffix.lower() in TEXT_EXT or any(low.startswith(n) for n in TEXT_NAMES):
                p = root / rel / f
                try:
                    texts.append({"file": p.relative_to(root).as_posix(),
                                  "text": p.read_text(encoding="utf-8", errors="replace")[:max_text_chars]})
                except Exception as e:  # noqa: BLE001
                    texts.append({"file": p.relative_to(root).as_posix(), "error": str(e)})

    return {"root": str(root), "n_files": sum(by_ext.values()), "by_extension": dict(by_ext),
            "folders": folder_view, "audio": audio, "tables": tables, "texts": texts}
