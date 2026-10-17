"""Headless evidence run: the agent (Gemini) plans checks per folder, runs the acoustic tools, writes a proposal file.

Nothing is applied: the output is <NORMALIZER_WORK_DIR>/proposals/<source_id>.json (status pending_approval) with the
check plan, candidates + evidence + confidence + questions, and the files that failed. A person approves entries,
then scripts/apply_proposals.py turns them into mapping interval_labels / unresolved questions.

Credentials (names only; values are never printed or written): GOOGLE_API_KEY, or GOOGLE_API_KEY_FILE (path to a
text file holding the key), or GOOGLE_GENAI_USE_VERTEXAI=TRUE with GOOGLE_CLOUD_PROJECT.
Optional: NORMALIZER_MODEL (default gemini-3.8-flash), NORMALIZER_WORK_DIR (default ./work).

Usage:
  python scripts/propose_evidence.py <source_dir> [--source-id ID] [--mapping approved_mapping.json]
                                     [--kaggle owner/slug] [--max-files 8] [--log run.log]

--mapping: the claimed conditions per folder (N, Z, D, Workpiece, ...) are read from the mapping's field rules
(path regex or constant) and given to the agent as claims. A folder holding label.csv (MSM layout) is run in
consistency_only mode: its label.csv rows are given to the agent, which checks them and proposes no labels.

Exit codes: 0 proposal saved; 1 model/runtime error or a forbidden tool call (convert_source, load_tables,
save_mapping); 2 the agent saved no valid proposal file; 3 no credentials / agent not loadable.
A failed run writes nothing to the work folder: EVERY agent write of the run (save_proposals, render_session_figure
and even a forbidden save_mapping) goes to a temporary staging folder (proposals.route_writes_to), and only the
proposal file and its figures are copied to the work folder on success.
The message lists each folder's file count and 3 example names (first, second, last), not every file name; the
agent can call inspect_source for more. For a labelled (MSM) folder the label.csv boundaries are also registered
with save_proposals, which audits every detect_change_points window against them (checked / matched / missed).
"""
import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))

AUDIO_EXT = {".wav", ".flac"}
FORBIDDEN_CALLS = {"convert_source", "load_tables", "save_mapping"}
ACOUSTIC = {"audio_profile", "detect_change_points", "estimate_spindle_speed", "class_separability"}

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("source_dir")
ap.add_argument("--source-id", default=None)
ap.add_argument("--mapping", default=None, help="approved mapping: claimed conditions per folder from its field rules")
ap.add_argument("--kaggle", default=None)
ap.add_argument("--max-files", type=int, default=8, help="files the agent may check per folder (default 8)")
ap.add_argument("--log", default=None, help="also write the run log to this file")
ap.add_argument("--print-prompt", action="store_true", help="print the message the agent would get and exit (no model call)")
a = ap.parse_args()

_lines: list = []


def _redact(text: str) -> str:
    key = os.environ.get("GOOGLE_API_KEY")
    return text.replace(key, "***") if key else text


def out(line: str = "") -> None:
    line = _redact(str(line))
    print(line, flush=True)
    _lines.append(line)


def finish(code: int) -> None:
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        Path(a.log).write_text("\n".join(_lines) + "\n", encoding="utf-8")
    sys.exit(code)


# --- credential preflight (FEA-011): before importing the agent ---
vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes")
if not a.print_prompt and not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY_FILE")
                              or (vertex and os.environ.get("GOOGLE_CLOUD_PROJECT"))):
    out("NOT RUN: no Gemini credentials. Set GOOGLE_API_KEY or GOOGLE_API_KEY_FILE "
        "(or GOOGLE_GENAI_USE_VERTEXAI=TRUE with GOOGLE_CLOUD_PROJECT). No proposal file was written.")
    finish(3)

src = Path(a.source_dir).resolve()
if not src.is_dir():
    out(f"NOT RUN: not a directory: {src}")
    finish(3)

try:
    from google.adk.runners import InMemoryRunner  # noqa: E402
    from google.genai import types  # noqa: E402

    import cutsound_normalizer.agent as agent_mod  # noqa: E402  (loads GOOGLE_API_KEY_FILE -> GOOGLE_API_KEY)
    from cutsound_normalizer import proposals as prop_mod  # noqa: E402
    from cutsound_normalizer.mapping_schema import Mapping  # noqa: E402
    from cutsound_normalizer.tools import figure as fig_mod  # noqa: E402
    from cutsound_normalizer.tools.convert import _field_values  # noqa: E402
    import soundfile as sf  # noqa: E402
except Exception as e:  # noqa: BLE001
    out(f"NOT RUN: agent could not be loaded ({type(e).__name__}: {e}). No proposal file was written.")
    finish(3)

root_agent = agent_mod.root_agent


# ------------------------------------------------------------------------------------ conditions
def _skipped(rel: str) -> bool:
    return any(part.startswith(("_", ".")) for part in rel.split("/"))


def collect_folders(root: Path, mapping) -> dict:
    """{folder_rel: [file_rel, ...]} of audio files (mapping audio_glob if given), '_'/'.' paths skipped."""
    if mapping is not None:
        files = [p for p in root.glob(mapping.session_rule.audio_glob) if p.is_file()]
    else:
        files = [p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXT]
    groups = defaultdict(list)
    for p in sorted(files):
        rel = p.relative_to(root).as_posix()
        if _skipped(rel):
            continue
        groups[p.parent.relative_to(root).as_posix() or "."].append(rel)
    return dict(sorted(groups.items()))


def label_rows(folder: Path) -> list:
    p = folder / "label.csv"
    if not p.is_file():
        return []
    with open(p, encoding="utf-8-sig") as fh:
        return [{"start": float(r["start"]), "end": float(r["end"]), "label": int(r["label"]),
                 "N": float(r["N"]) if r.get("N") else None, "Z": int(float(r["Z"])) if r.get("Z") else None}
                for r in csv.DictReader(fh)]


mapping = None
if a.mapping:
    mapping = Mapping.model_validate_json(Path(a.mapping).read_text(encoding="utf-8"))
folders = collect_folders(src, mapping)
if not folders:
    out(f"NOT RUN: no audio files under {src}")
    finish(3)

msm_layout = {f: label_rows(src / f) for f in folders if (src / f / "label.csv").is_file()}
mode = "consistency_only" if msm_layout else "label_candidates"
source_id = a.source_id or (mapping.source_id if mapping else re.sub(r"[^A-Za-z0-9_]+", "_", "_".join(src.parts[-2:])).lower())

def _examples(names: list) -> str:
    """3 example names (first, second, last) instead of the whole listing (token cost)."""
    ex = names if len(names) <= 3 else [names[0], names[1], "...", names[-1]]
    return ", ".join(ex)


conditions = {}
label_boundaries = {}          # airborne wav (absolute) -> label.csv start/end times, for the boundary audit
if mapping is not None:
    conditions["mapping"] = {"file": str(Path(a.mapping).resolve()), "source_id": mapping.source_id,
                             "machine": mapping.machine.model_dump(), "approved_by": mapping.approved_by}
lines = []
for fol, files in folders.items():
    cond = {}
    if mapping is not None:
        fv = _field_values(mapping, files[0])
        cond = {k: v for k, v in fv.items() if k in ("N", "Z", "D", "Workpiece", "Tool") and v not in ("", None)}
        unknown = [k for k in ("N", "Z", "D") if k not in cond]
        if unknown:
            cond["unknown"] = unknown
    conditions.setdefault("folders", {})[fol] = cond
    names = [Path(f).name for f in files]
    if fol in msm_layout:
        rows = msm_layout[fol]
        s0 = [f for f in files if f.endswith("_s0.wav")] or files
        try:
            dur = round(sf.info(str(src / s0[0])).duration, 3)
        except Exception:  # noqa: BLE001
            dur = None
        rows_txt = "; ".join(f"{r['start']:.3f}-{r['end']:.3f} label {r['label']} N {r['N']:g} Z {r['Z']}" for r in rows)
        others = [n for n in names if n != Path(s0[0]).name]
        label_boundaries[str(src / s0[0])] = [r["start"] for r in rows] + [r["end"] for r in rows]
        lines.append(f"- {fol}: MSM session WITH label.csv ({len(rows)} rows). Airborne file: {s0[0]} ({dur} s); "
                     f"other audio ({len(others)}): {_examples(others) or 'none'}. label.csv rows "
                     f"(start-end s, label, N rpm, Z): {rows_txt}")
    else:
        claim = ", ".join(f"{k}={v}" for k, v in cond.items()) or "no mapping given (claims only from names)"
        lines.append(f"- {fol}: {len(files)} audio files, named like {_examples(names)} (sorted); "
                     f"claimed conditions: {claim}")

msg = (f"EVIDENCE RUN. Source folder (absolute): {src}\nsource_id: '{source_id}'. mode: {mode}.\n"
       f"Use absolute paths = source folder + '/' + the relative path. At most {a.max_files} files per folder.\n")
if mapping is not None:
    m = mapping.machine
    msg += (f"Claimed conditions come from the approved mapping {Path(a.mapping).name} (field rules; claims, not facts): "
            f"machine {m.machine_id} ({m.process}), coolant {m.coolant}, field {m.field}.\n")
if a.kaggle:
    msg += f"The data came from Kaggle dataset {a.kaggle}.\n"
msg += "Folders (relative to the source folder):\n" + "\n".join(lines) + "\n"
msg += ("Follow the EVIDENCE RUN workflow: write the CHECK PLAN first, run it, then call save_proposals once with "
        f"source_id '{source_id}', source_dir '{src.as_posix()}', mode '{mode}'. Do not convert, do not save a mapping, "
        "do not write labels or change N.")
if a.print_prompt:
    out(msg)
    finish(0)


# ------------------------------------------------------------------------------------ run
def _digest(name: str, r: dict) -> str:
    if not isinstance(r, dict):
        return type(r).__name__
    if r.get("status") == "error":
        return f"ERROR {str(r.get('error'))[:160]}"
    if name == "estimate_spindle_speed":
        cc = r.get("claimed_check") or {}
        return (f"status={r.get('status')} best={r.get('best_rpm')} claim={r.get('claimed_N')} check={cc.get('status')} "
                f"amb={r.get('harmonic_ambiguity')} top={[c.get('rpm') for c in (r.get('candidates') or [])[:4]]}")
    if name == "detect_change_points":
        return (f"status={r.get('status')} dur={r.get('duration_s')} points="
                f"{[(c.get('t_s'), c.get('scale_s')) for c in (r.get('change_points') or [])][:12]}")
    if name == "class_separability":
        return f"verdict={r.get('verdict')} n={r.get('n_a')}/{r.get('n_b')} best_band={r.get('best_band_index')} best_auc={r.get('best_auc')}"
    if name == "audio_profile":
        s = r.get("summary") or {}
        return f"status={r.get('status')} rms_db={s.get('rms_db')} clipped={r.get('clipped')}"
    if name == "save_proposals":
        return (f"status={r.get('status')} n={r.get('n_proposals')} kinds={r.get('kinds')} errors_recorded="
                f"{r.get('n_errors_recorded')} warnings={len(r.get('evidence_warnings') or [])}"
                + (f" errors={str(r.get('errors'))[:600]}" if r.get("errors") else ""))
    if name == "render_session_figure":
        return f"status={r.get('status')} png={Path(str(r.get('png_path'))).name if r.get('png_path') else None}"
    return f"keys={list(r.keys())[:8]}"


def _short_args(name: str, args: dict) -> str:
    if name == "save_proposals":
        return f"(proposals_json: {len(str(args.get('proposals_json', '')))} chars)"
    s = json.dumps(args, ensure_ascii=False)
    return _redact(s.replace(str(src).replace("\\", "\\\\"), "<src>").replace(src.as_posix(), "<src>"))[:220]


async def run(staging: Path) -> dict:
    prop_mod.route_writes_to(staging)        # proposals, figures AND save_mapping write into staging only
    prop_mod.SOURCE_ROOT = src
    prop_mod.MODEL_NAME = agent_mod.MODEL
    prop_mod.reset_tool_log()
    prop_mod.set_label_boundaries(label_boundaries)
    runner = InMemoryRunner(agent=root_agent, app_name="cutsound_normalizer")
    sess = await runner.session_service.create_session(app_name="cutsound_normalizer", user_id="evidence")
    st = {"tools": [], "final": [], "saved": None, "forbidden": [], "error": None,
          "usage": {"prompt": 0, "candidates": 0, "total": 0, "calls": 0}}
    async for ev in runner.run_async(user_id="evidence", session_id=sess.id,
                                     new_message=types.Content(role="user", parts=[types.Part(text=msg)])):
        if ev.error_code or ev.error_message:
            st["error"] = f"{ev.error_code}: {ev.error_message}"
        um = ev.usage_metadata
        if um is not None and ev.author == root_agent.name:
            st["usage"]["prompt"] += um.prompt_token_count or 0
            st["usage"]["candidates"] += um.candidates_token_count or 0
            st["usage"]["total"] += um.total_token_count or 0
            st["usage"]["calls"] += 1
        for p in (ev.content.parts if ev.content and ev.content.parts else []):
            if p.function_call:
                name = p.function_call.name
                st["tools"].append(name)
                if name in FORBIDDEN_CALLS:
                    st["forbidden"].append(name)
                out(f"-> {name} {_short_args(name, dict(p.function_call.args or {}))}")
            elif p.function_response:
                name = p.function_response.name
                resp = p.function_response.response or {}
                out(f"<- {name} {_digest(name, resp)}")
                if name == "save_proposals" and resp.get("status") == "saved_pending_approval":
                    st["saved"] = Path(resp["path"])
            elif p.text and ev.author == root_agent.name:
                st["final"].append(p.text)
    return st


def _rewrite(obj, old: str, new: str):
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, dict):
        return {k: _rewrite(v, old, new) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite(v, old, new) for v in obj]
    return obj


final_root = Path(agent_mod.WORK_DIR)
out(f"model={agent_mod.MODEL} date={dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
out(f"source={src} source_id={source_id} mode={mode} folders={len(folders)} mapping={a.mapping}")
t_start = time.time()
with tempfile.TemporaryDirectory(prefix="evidence_") as tmp:
    staging = Path(tmp)
    try:
        st = asyncio.run(run(staging))
    except Exception as e:  # noqa: BLE001
        out(f"FAILED: run raised {type(e).__name__}: {_redact(str(e))[:800]}. No proposal file was written.")
        finish(1)
    wall = round(time.time() - t_start, 1)
    out("== tool sequence: " + " -> ".join(st["tools"]))
    counts = {n: st["tools"].count(n) for n in dict.fromkeys(st["tools"])}
    out(f"== tool counts: {counts}")
    u = st["usage"]
    out(f"== token usage: prompt={u['prompt']} candidates={u['candidates']} total={u['total']} "
        f"model_calls={u['calls']} wall_s={wall}")
    errs = prop_mod.tool_errors()
    out(f"== tool errors (recorded, run continued): {len(errs)}" + "".join(f"\n   {x['tool']} {x['path']}: {x['error'][:160]}" for x in errs))
    out("== agent reply\n" + "\n".join(st["final"])[-6000:])
    if st["error"]:
        out(f"FAILED: model returned an error ({st['error']}). No proposal file was written.")
        finish(1)
    if st["forbidden"]:
        out(f"FAILED: forbidden tool call(s) during an evidence run: {st['forbidden']}. No proposal file was written.")
        finish(1)
    if not st["saved"] or not st["saved"].is_file():
        out("FAILED: the agent did not save a valid proposal file. No proposal file was written.")
        finish(2)
    data = json.loads(st["saved"].read_text(encoding="utf-8"))
    final_dir = final_root / "proposals"
    final_dir.mkdir(parents=True, exist_ok=True)
    figs_src, figs_dst = staging / "figures", final_root / "figures"
    if figs_src.is_dir():
        figs_dst.mkdir(parents=True, exist_ok=True)
        for f in figs_src.iterdir():
            shutil.copyfile(f, figs_dst / f.name)
        data = _rewrite(data, str(figs_src), str(figs_dst))
    dest = final_dir / st["saved"].name
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    run_rec = {"model": agent_mod.MODEL, "date": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "source": str(src), "source_id": source_id, "mode": mode, "mapping": a.mapping,
               "tool_sequence": st["tools"], "tool_counts": counts, "usage": u, "wall_s": wall,
               "tool_errors": errs, "message_chars": len(msg)}
    (final_dir / f"{dest.stem}.run.json").write_text(json.dumps(run_rec, ensure_ascii=False, indent=1), encoding="utf-8")
    out("== check plan (as saved)")
    for c in data.get("check_plan", []):
        out(f"   {c['folder']} | {c['folder_type']} | {', '.join(c['tools'])} | {len(c.get('files') or [])} files | {c['reason'][:160]}")
    for w in data.get("boundary_audit") or []:
        mine = [b for b in data.get("boundary_windows") or [] if abs(b["t0_s"] - w["t0_s"]) <= 0.5
                and abs(b["t1_s"] - w["t1_s"]) <= 0.5]
        out(f"== boundary audit {w['t0_s']:g}-{w['t1_s']:g} s: checked {w['n_checked']} / matched {w['n_matched']} / "
            f"missed {w['n_missed']} (tol {w['tolerance_s']} s, edge {w['edge_s']} s); points {w['n_points']}, "
            f"off-boundary {w['n_points_off_boundary']} | agent recorded: "
            + (f"{mine[0]['n_checked']} / {mine[0]['n_matched']} / {mine[0]['n_missed']} result {mine[0]['result']}"
               if mine else "nothing"))
    if data.get("counts"):
        out(f"== counts (computed by save_proposals): {json.dumps(data['counts'], ensure_ascii=False)}")
    out(f"== proposal saved: {dest} ({len(data.get('proposals', []))} proposals, {len(data.get('consistency', []))} "
        f"consistency items, {len(data.get('errors', []))} errors) status=pending_approval - nothing applied")
finish(0)
