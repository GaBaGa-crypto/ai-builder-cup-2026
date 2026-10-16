"""load_tables: turn a converted source's manifest into tables (local parquet always; BigQuery when credentials exist)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pandas as pd

ANALYSIS_TABLES = ("sources", "sessions", "events", "context")
PROVENANCE_TABLES = ("provenance",)

# Fixed column types so every source appends to the same BigQuery schema (sources differ: '' vs 520 vs '10000').
FLOAT_COLS = {
    "events": ("dur_s", "start_s", "end_s", "N", "ap", "ae", "F", "D", "clip_ratio", "rms_dbfs", "peak_dbfs"),
    "sessions": ("duration_s", "channel_corr", "clip_ratio_native", "peak_dbfs", "rms_dbfs"),
    "context": ("time_s", "spindle_speed_rpm", "feed_rate_mm_s", "spindle_load_pct", "axis_load_x_pct",
                "axis_load_y_pct", "axis_load_z_pct", "power_w"),
}
INT_COLS = {
    "events": ("msm_label", "Z"),
    "sessions": ("native_sample_rate", "native_channels", "int16_clipped_by_conversion", "n_label_rows", "n_events"),
    "sources": ("n_sessions",),
}


def _read_jsonl(p: Path) -> pd.DataFrame:
    if not p.exists():
        return pd.DataFrame()
    return pd.DataFrame([json.loads(line) for line in open(p, encoding="utf-8")])


def _frames(man: Path) -> dict:
    fr = {name: _read_jsonl(man / f"{name}.jsonl") for name in ("sources", "sessions", "events", "provenance")}
    ctx = man / "context.csv"
    fr["context"] = pd.read_csv(ctx) if ctx.exists() else pd.DataFrame()
    for name, df in fr.items():
        if df.empty:
            continue
        for c in FLOAT_COLS.get(name, ()):
            if c not in df.columns:
                df[c] = None
            df[c] = pd.to_numeric(df[c].replace("", None), errors="coerce").astype("float64")
        for c in INT_COLS.get(name, ()):
            if c not in df.columns:
                df[c] = None
            df[c] = pd.to_numeric(df[c].replace("", None), errors="coerce").round().astype("Int64")
        for c in df.columns:
            if c in FLOAT_COLS.get(name, ()) or c in INT_COLS.get(name, ()):
                continue
            if df[c].dtype == object or name == "provenance":
                df[c] = df[c].map(lambda v: json.dumps(v) if isinstance(v, (list, dict)) else v).astype("string")
    return fr


def load_tables(out_dir: str, source_id: str, project: Optional[str] = None, dataset: str = "msm_norm",
                provenance_dataset: str = "msm_provenance", location: str = "US") -> dict:
    """Write normalized tables for one source.

    Always writes parquet under <out_dir>/<source_id>/_tables/. Loads into BigQuery only when a project is given
    (or GOOGLE_CLOUD_PROJECT is set) and application-default credentials work. Provenance goes to a separate dataset
    so analysis tools never see leak-prone fields.

    Returns:
        dict with row counts, local paths, and BigQuery status (loaded / skipped with reason).
    """
    man = Path(out_dir) / source_id / "_manifest"
    if not man.exists():
        return {"error": f"no manifest for {source_id}; run convert_source first"}
    frames = _frames(man)
    tdir = Path(out_dir) / source_id / "_tables"
    tdir.mkdir(parents=True, exist_ok=True)
    local = {}
    for name, df in frames.items():
        if df.empty:
            continue
        p = tdir / f"{name}.parquet"
        df.to_parquet(p, index=False)
        local[name] = {"rows": len(df), "path": str(p)}

    project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        return {"source_id": source_id, "local": local,
                "bigquery": {"status": "skipped", "reason": "no GCP project configured (set GOOGLE_CLOUD_PROJECT)"}}
    try:
        import google.auth
        from google.cloud import bigquery
        creds, _ = google.auth.default()
        client = bigquery.Client(project=project, credentials=creds, location=location)
    except Exception as e:  # noqa: BLE001
        return {"source_id": source_id, "local": local,
                "bigquery": {"status": "skipped", "reason": f"credentials unavailable: {type(e).__name__}: {e}"}}

    loaded = {}
    for ds_name, names in ((dataset, ANALYSIS_TABLES), (provenance_dataset, PROVENANCE_TABLES)):
        client.create_dataset(bigquery.Dataset(f"{project}.{ds_name}"), exists_ok=True)
        for name in names:
            df = frames.get(name)
            if df is None or df.empty:
                continue
            table_id = f"{project}.{ds_name}.{name}"
            try:  # replace this source's rows, keep other sources
                client.query(f"DELETE FROM `{table_id}` WHERE source_id = @s" if "source_id" in df.columns
                             else f"DELETE FROM `{table_id}` WHERE session_id LIKE @p",
                             job_config=bigquery.QueryJobConfig(query_parameters=[
                                 bigquery.ScalarQueryParameter("s", "STRING", source_id),
                                 bigquery.ScalarQueryParameter("p", "STRING", f"{source_id}__%")])).result()
            except Exception:  # noqa: BLE001  table may not exist yet
                pass
            job = client.load_table_from_dataframe(
                df, table_id, job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND",
                                                                schema_update_options=["ALLOW_FIELD_ADDITION"]))
            job.result()
            loaded[table_id] = len(df)
    return {"source_id": source_id, "local": local, "bigquery": {"status": "loaded", "tables": loaded}}
