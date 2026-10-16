"""Run convert -> validate -> tables without the LLM (used for tests and for re-running an approved mapping).

Usage:
  python scripts/run_pipeline.py <source_dir> <mapping.json> <out_dir>   (from the repo root) [--limit N] [--approved-by NAME]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from cutsound_normalizer.tools import convert_source, load_tables, validate_normalized  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("source_dir")
ap.add_argument("mapping")
ap.add_argument("out_dir")
ap.add_argument("--limit", type=int, default=None)
ap.add_argument("--approved-by", default="")
a = ap.parse_args()

conv = convert_source(a.source_dir, a.mapping, a.out_dir, limit=a.limit, approved_by=a.approved_by)
print("== convert", json.dumps(conv, ensure_ascii=False, indent=1))
if "error" in conv:
    sys.exit(1)
val = validate_normalized(a.out_dir, conv["source_id"])
print("== validate", json.dumps(val, ensure_ascii=False, indent=1))
tab = load_tables(a.out_dir, conv["source_id"])
print("== tables", json.dumps(tab, ensure_ascii=False, indent=1))
sys.exit(1 if val["n_errors"] else 0)
