"""ADK agent: understands an unfamiliar cutting-sound dataset and normalizes it to the MSM specification.

Gemini reads the folder summary and README, proposes a field-by-field mapping (structured), and explains open
questions. Deterministic tools do every data change. Conversion and table loading require human confirmation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool

from .credentials import load_api_key_from_file
from .mapping_schema import Mapping

load_api_key_from_file()  # GOOGLE_API_KEY_FILE -> GOOGLE_API_KEY (value is never printed or written)
from .tools import convert_source, inspect_source, kaggle_dataset_info, load_tables, validate_normalized

SPEC_PATH = Path(__file__).resolve().parent / "spec" / "msm_spec.json"
WORK_DIR = Path(os.environ.get("NORMALIZER_WORK_DIR", "work"))
MODEL = os.environ.get("NORMALIZER_MODEL", "gemini-3.8-flash")


def get_msm_spec() -> dict:
    """Return the MSM target specification: audio format, label.csv columns with units, label values,
    unified controller columns, provenance-only fields and unit conversions."""
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _inline_refs(node, defs):
    """Replace JSON-schema '$ref' pointers with the referenced definition and drop '$defs'
    (the Gemini API rejects '$ref'/'$defs' inside a function response)."""
    if isinstance(node, dict):
        if "$ref" in node:
            target = defs[node["$ref"].split("/")[-1]]
            return _inline_refs({**target, **{k: v for k, v in node.items() if k != "$ref"}}, defs)
        return {k: _inline_refs(v, defs) for k, v in node.items() if k != "$defs"}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def get_mapping_schema() -> dict:
    """Return the JSON schema that a mapping proposal must follow."""
    schema = Mapping.model_json_schema()
    return _inline_refs(schema, schema.get("$defs", {}))


def save_mapping(mapping_json: str) -> dict:
    """Validate a mapping proposal against the schema and save it for human review.

    Args:
        mapping_json: The full mapping as a JSON string.

    Returns:
        dict with saved path, or validation errors to fix.
    """
    try:
        m = Mapping.model_validate_json(mapping_json)
    except Exception as e:  # noqa: BLE001
        return {"status": "invalid", "errors": str(e)[:3000]}
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    p = WORK_DIR / "mappings" / f"{m.source_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(m.model_dump_json(indent=1), encoding="utf-8")
    return {"status": "saved_pending_approval", "path": str(p), "n_label_rules": len(m.label_rules),
            "n_field_rules": len(m.fields), "unresolved": m.unresolved}


INSTRUCTION = """You are the Data Normalization Agent for a machining-sound anomaly project.
Goal: turn an unfamiliar cutting-sound dataset into the MSM specification so it can be analyzed together with MSM.

Work in this order:
1. Call get_msm_spec and get_mapping_schema once.
2. Call inspect_source on the folder the user gives. Read folder names, file names, audio formats, table headers and README text.
   If the data came from Kaggle (the user gives an owner/slug or URL), also call kaggle_dataset_info: downloads do not include
   the dataset page text, which is often the only place that states machine, tool, workpiece and what each folder means.
3. Propose a mapping that follows the schema exactly. For every field (N, ap, ae, F, Z, D, Tool, Workpiece, direction) state
   where the value comes from (constant from README, regex on the path, or unknown) and quote the evidence.
   Convert units to the MSM units in the spec (feed in mm/s, lengths in mm, speed in rpm) using the factor field.
4. Label rules are the most important judgment. Follow the spec exactly:
   - 0 only for stable normal cutting; 1 only for cutting that should be stopped; 2 only for a pre-existing tool defect
     while the cutting itself is normal.
   - Tool wear is NOT automatically an anomaly. A broken tool is NOT automatically label 1 or 2.
     If the source does not say whether the cutting itself was abnormal, use -1 and add a question to 'unresolved';
     the question must list the candidates that remain possible (e.g. a worn tool with an acceptable cut is 2,
     a cut that should have been stopped is 1, stable cutting is 0).
   - Idle or machine-only sound is cut_state 'non_cutting' with label -1.
5. Call save_mapping. Then show the user a short table of the label rules and every unresolved question, and ask for approval.
6. Only after the user approves: call convert_source (it asks for confirmation), then validate_normalized,
   then load_tables. Report counts and any validation errors exactly as returned. Never claim BigQuery loading succeeded
   unless load_tables says status 'loaded'.
Never invent values that are not in the source. This includes machine.field (lab/industry), machine.coolant and
audio.mic_type: if the source does not state them, use 'unknown'. Copy strings (workpiece, model) verbatim from the
source, keeping its spelling and spaces. Ignore folders whose name starts with '_' or '.'; they are not part of the
dataset. Never put program names, NC text, remarks or raw tool codes into fields used for analysis; they belong to
provenance only. Answer the user in their language; write mapping content in English."""


def _bigquery_tools() -> list:
    """Read-only BigQuery toolset when a project is configured; otherwise none."""
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return []
    try:
        import google.auth
        from google.adk.tools.bigquery import BigQueryCredentialsConfig, BigQueryToolset
        from google.adk.tools.bigquery.config import BigQueryToolConfig, WriteMode
        creds, _ = google.auth.default()
        return [BigQueryToolset(credentials_config=BigQueryCredentialsConfig(credentials=creds),
                                bigquery_tool_config=BigQueryToolConfig(write_mode=WriteMode.BLOCKED,
                                                                        default_dataset_id="msm_norm"))]
    except Exception:  # noqa: BLE001
        return []


root_agent = LlmAgent(
    name="msm_normalizer",
    model=MODEL,
    description="Normalizes external cutting-sound datasets into the MSM specification.",
    instruction=INSTRUCTION,
    tools=[
        FunctionTool(get_msm_spec),
        FunctionTool(get_mapping_schema),
        FunctionTool(inspect_source),
        FunctionTool(kaggle_dataset_info),
        FunctionTool(save_mapping),
        FunctionTool(convert_source, require_confirmation=True),
        FunctionTool(validate_normalized),
        FunctionTool(load_tables, require_confirmation=True),
        *_bigquery_tools(),
    ],
)
