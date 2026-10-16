"""Ask the agent (Gemini) to propose a mapping for one source, headless. Stops before conversion.

Credentials (names only; values are never printed or written):
  GOOGLE_API_KEY            Gemini API key, or
  GOOGLE_API_KEY_FILE       path to a text file whose first non-empty line is the key, or
  GOOGLE_GENAI_USE_VERTEXAI=TRUE with GOOGLE_CLOUD_PROJECT (+ GOOGLE_CLOUD_LOCATION) for Vertex AI.
Optional: NORMALIZER_MODEL (default gemini-3.8-flash), NORMALIZER_WORK_DIR (default ./work).

Usage:
  python scripts/propose_mapping.py <source_dir> [--kaggle owner/slug] [--source-id ID] [--log run.log]

On success writes <NORMALIZER_WORK_DIR>/mappings/<source_id>.json with status saved_pending_approval and exits 0.
The proposal is never applied: a human reviews it, sets approved_by, then runs scripts/normalize.py.
A failed run (no credentials, API error, no valid proposal saved) exits non-zero and never creates or overwrites the
proposal file: the agent saves into a temporary folder and the file is copied to the work folder only on success.
"""
import argparse
import asyncio
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

ap = argparse.ArgumentParser()
ap.add_argument("source_dir")
ap.add_argument("--kaggle", default=None)
ap.add_argument("--source-id", default=None)
ap.add_argument("--log", default=None, help="Also write the tool sequence and final reply to this file")
a = ap.parse_args()

_log_lines: list = []


def _redact(text: str) -> str:
    key = os.environ.get("GOOGLE_API_KEY")
    return text.replace(key, "***") if key else text


def out(line: str = "") -> None:
    line = _redact(str(line))
    print(line, flush=True)
    _log_lines.append(line)


def finish(code: int) -> None:
    if a.log:
        Path(a.log).parent.mkdir(parents=True, exist_ok=True)
        Path(a.log).write_text("\n".join(_log_lines) + "\n", encoding="utf-8")
    sys.exit(code)


# --- credential preflight (before importing the agent, which reads GOOGLE_API_KEY_FILE) ---
vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() in ("1", "true", "yes")
if not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY_FILE")
        or (vertex and os.environ.get("GOOGLE_CLOUD_PROJECT"))):
    out("NOT RUN: no Gemini credentials. Set GOOGLE_API_KEY or GOOGLE_API_KEY_FILE "
        "(or GOOGLE_GENAI_USE_VERTEXAI=TRUE with GOOGLE_CLOUD_PROJECT). No proposal was written.")
    finish(3)

try:
    from google.adk.runners import InMemoryRunner  # noqa: E402
    from google.genai import types  # noqa: E402

    import cutsound_normalizer.agent as agent_mod  # noqa: E402  (loads GOOGLE_API_KEY_FILE -> GOOGLE_API_KEY)
except Exception as e:  # noqa: BLE001
    out(f"NOT RUN: agent could not be loaded ({type(e).__name__}: {e}). No proposal was written.")
    finish(3)

root_agent = agent_mod.root_agent
final_dir = Path(agent_mod.WORK_DIR) / "mappings"



# agent.get_mapping_schema already serves the schema with '$ref'/'$defs' inlined (the Gemini API rejects them).

msg = f"Propose an MSM mapping for the dataset in folder: {a.source_dir}."
if a.kaggle:
    msg += f" It was downloaded from Kaggle dataset {a.kaggle}."
if a.source_id:
    msg += f" Use source_id '{a.source_id}'."
msg += " Save the mapping with save_mapping, then stop and list the unresolved questions. Do not convert."


def _short_args(name: str, args: dict) -> str:
    if name == "save_mapping":
        return f"(mapping_json: {len(str(args.get('mapping_json', '')))} chars)"
    return json.dumps(args, ensure_ascii=False)[:200]


async def run(staging: Path) -> dict:
    agent_mod.WORK_DIR = staging  # save_mapping reads this global at call time
    runner = InMemoryRunner(agent=root_agent, app_name="cutsound_normalizer")
    sess = await runner.session_service.create_session(app_name="cutsound_normalizer", user_id="eval")
    st = {"tools": [], "final": "", "saved": None, "stopped_for_confirmation": None,
          "usage": {"prompt": 0, "candidates": 0, "total": 0, "calls": 0}, "error": None}
    async for ev in runner.run_async(user_id="eval", session_id=sess.id,
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
                out(f"-> tool {name} {_short_args(name, dict(p.function_call.args or {}))}")
                if name == "adk_request_confirmation":
                    st["stopped_for_confirmation"] = name
            elif p.function_response:
                name = p.function_response.name
                resp = p.function_response.response or {}
                if name == "save_mapping":
                    out(f"<- save_mapping status={resp.get('status')} n_label_rules={resp.get('n_label_rules')} "
                        f"n_field_rules={resp.get('n_field_rules')}")
                    if resp.get("status") == "saved_pending_approval":
                        st["saved"] = Path(resp["path"])
                    elif resp.get("errors"):
                        out(f"   errors: {str(resp.get('errors'))[:400]}")
                else:
                    keys = list(resp.keys())[:8] if isinstance(resp, dict) else type(resp).__name__
                    out(f"<- {name} keys={keys}" + (f" error={resp.get('error')}" if isinstance(resp, dict)
                                                     and resp.get("error") else ""))
            elif p.text and ev.author == root_agent.name:
                st["final"] = p.text
    return st


out(f"model={agent_mod.MODEL} date={dt.datetime.now().astimezone().isoformat(timespec='seconds')}")
out(f"source={a.source_dir} kaggle={a.kaggle} source_id={a.source_id}")
with tempfile.TemporaryDirectory(prefix="propose_") as tmp:
    try:
        st = asyncio.run(run(Path(tmp)))
    except Exception as e:  # noqa: BLE001
        out(f"FAILED: Gemini run raised {type(e).__name__}: {str(e)[:800]}. No proposal was written.")
        finish(1)
    out("== tool sequence: " + " -> ".join(st["tools"]))
    u = st["usage"]
    out(f"== token usage: prompt={u['prompt']} candidates={u['candidates']} total={u['total']} model_calls={u['calls']}")
    out("== agent reply\n" + st["final"])
    if st["error"]:
        out(f"FAILED: model returned an error ({st['error']}). No proposal was written.")
        finish(1)
    if st["stopped_for_confirmation"]:
        out("NOTE: the agent requested confirmation for a data-changing tool; nothing was confirmed.")
    if not st["saved"] or not st["saved"].is_file():
        out("FAILED: the agent did not save a schema-valid mapping. No proposal was written.")
        finish(2)
    final_dir.mkdir(parents=True, exist_ok=True)
    dest = final_dir / st["saved"].name
    shutil.copyfile(st["saved"], dest)
    out(f"== proposal saved: {dest} status=saved_pending_approval (not applied; needs human approval)")
finish(0)
