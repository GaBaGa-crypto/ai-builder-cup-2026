"""One-shot offline entry point: approved mapping -> convert -> validate -> tables [-> build_db].

No LLM is called here. The mapping must already be approved by a person (the agent proposes it; see agent/README.md).

Usage (from the repo root):
  python scripts/normalize.py --source <dataset_dir> --mapping agent/mappings/ctwa_reference.json --out <norm_dir>
                              [--approved-by NAME] [--db <db_dir> --msm-raw <MSM raw dir>]

Output: <out>/<source_id>/<machine_id>/dataset<N>/ (MSM raw layout), manifests and parquet tables under
<out>/<source_id>/, and the step log <out>/<source_id>/_manifest/normalize_run.json.
With --db, runs proto/scripts/build_db.py <msm-raw> <db> --extra-raw <out>/<source_id>.

Exit codes: 0 ok, 1 a step failed (conversion error or validation errors), 2 build_db does not support --extra-raw yet.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
from cutsound_normalizer.tools import convert_source, load_tables, validate_normalized  # noqa: E402

BUILD_DB = REPO / "proto" / "scripts" / "build_db.py"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="root folder of the dataset to normalize")
    ap.add_argument("--mapping", required=True, help="approved mapping JSON")
    ap.add_argument("--out", required=True, help="root folder for normalized output")
    ap.add_argument("--approved-by", default="", help="person who approved the mapping (required if it has open questions)")
    ap.add_argument("--db", default=None, help="build the viewer databases here (needs --msm-raw)")
    ap.add_argument("--msm-raw", default=None, help="MSM raw root passed to build_db.py as <raw_dir>")
    a = ap.parse_args()
    if bool(a.db) != bool(a.msm_raw):
        ap.error("--db and --msm-raw must be given together")

    run = {"started_at": dt.datetime.now().isoformat(timespec="seconds"), "argv": sys.argv[1:],
           "source": str(Path(a.source).resolve()), "mapping": str(Path(a.mapping).resolve()),
           "out": str(Path(a.out).resolve()), "approved_by": a.approved_by, "steps": {}}
    run_path = None

    def save(status: str) -> None:
        run["status"] = status
        run["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
        if run_path is not None:
            run_path.parent.mkdir(parents=True, exist_ok=True)
            run_path.write_text(json.dumps(run, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    # 1. convert (deterministic execution of the approved mapping)
    conv = convert_source(a.source, a.mapping, a.out, approved_by=a.approved_by)
    run["steps"]["convert"] = conv
    if "error" in conv:
        print(f"[1/4 convert] FAILED: {conv['error']}")
        if conv.get("unresolved"):
            for q in conv["unresolved"]:
                print(f"    open question: {q}")
        return 1
    sid = conv["source_id"]
    src_out = Path(a.out) / sid
    run_path = src_out / "_manifest" / "normalize_run.json"
    print(f"[1/4 convert] ok sessions={conv['n_sessions']} events={conv['n_events']} "
          f"labels={conv['events_by_label']} unlabeled_cutting_spans={conv.get('unlabeled_cutting_spans', 0)} "
          f"warnings={conv['n_warnings']} -> {src_out}")
    print(f"    dataset<N> numbering: {conv.get('dataset_numbering')}; index {src_out / '_manifest' / 'dataset_index.csv'}")
    if conv.get("previous_output_moved_to"):
        print(f"    previous output moved to {conv['previous_output_moved_to']}")

    # 2. validate (stop on any error)
    val = validate_normalized(a.out, sid)
    run["steps"]["validate"] = val
    print(f"[2/4 validate] sessions={val['n_sessions']} n_errors={val['n_errors']} n_warnings={val['n_warnings']}")
    if val["n_errors"]:
        for e in val["errors"][:10]:
            print(f"    error: {e}")
        save("validate_failed")
        return 1

    # 3. tables (parquet always; BigQuery only with a configured project)
    tab = load_tables(a.out, sid)
    run["steps"]["tables"] = tab
    if "error" in tab:
        print(f"[3/4 tables] FAILED: {tab['error']}")
        save("tables_failed")
        return 1
    rows = {k: v["rows"] for k, v in tab.get("local", {}).items()}
    print(f"[3/4 tables] parquet {rows}; bigquery {tab['bigquery']['status']}"
          + (f" ({tab['bigquery'].get('reason')})" if tab["bigquery"].get("reason") else ""))

    # 4. build_db (optional; the --extra-raw flag belongs to proto/scripts/build_db.py)
    if not a.db:
        run["steps"]["build_db"] = {"status": "skipped", "reason": "--db not given"}
        print("[4/4 build_db] skipped (no --db)")
        save("ok")
        return 0
    not_ready = {"status": "not_ready",
                 "reason": f"{BUILD_DB.relative_to(REPO).as_posix()} does not accept --extra-raw yet"}
    if not BUILD_DB.exists() or "--extra-raw" not in BUILD_DB.read_text(encoding="utf-8", errors="ignore"):
        run["steps"]["build_db"] = not_ready
        print(f"[4/4 build_db] build_db not ready: {not_ready['reason']}")
        save("build_db_not_ready")
        return 2
    cmd = [sys.executable, "-X", "utf8", str(BUILD_DB), str(a.msm_raw), str(a.db), "--extra-raw", str(src_out)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    tail = (p.stdout + p.stderr).strip().splitlines()[-15:]
    run["steps"]["build_db"] = {"cmd": cmd, "returncode": p.returncode, "output_tail": tail}
    if p.returncode != 0 and "unrecognized arguments" in p.stderr:
        run["steps"]["build_db"].update(not_ready)
        print(f"[4/4 build_db] build_db not ready: {not_ready['reason']}")
        save("build_db_not_ready")
        return 2
    if p.returncode != 0:
        print(f"[4/4 build_db] FAILED (exit {p.returncode})")
        for line in tail:
            print(f"    {line}")
        save("build_db_failed")
        return 1
    print(f"[4/4 build_db] ok -> {a.db}" + (f" | {tail[-1]}" if tail else ""))
    save("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
