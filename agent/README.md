# cutsound-normalizer

An ADK agent that turns an unfamiliar machining-sound dataset (Kaggle or a factory folder) into the
**MSM specification**, so it can be analyzed together with the MSM dataset.

- **Gemini decides meaning**: reads the folder, file names, audio formats and the Kaggle page text, then proposes a
  field-by-field mapping (label, spindle speed, feed, depth of cut, tool, workpiece ...) with quoted evidence.
- **A human approves** the mapping. Anything the source does not state stays `-1` / blank and becomes an open question.
- **Deterministic tools change data**: audio to 48 kHz / 16-bit / mono, `label.csv` with the 12 MSM columns,
  manifests, validation, and tables in local parquet and BigQuery.

## Target format (MSM)

| Item | Value |
|---|---|
| Audio | `<session>_s0.wav`, 48 kHz, PCM 16-bit, mono, level preserved (no loudness normalization) |
| `label.csv` | `start,end,label,direction,N,ap,ae,F,Z,D,Tool,Workpiece`; N rpm, ap/ae/D mm, F mm/s |
| Labels | 0 normal, 1 abnormal (should stop), 2 defective tool but normal cut; `-1` = not mappable (events table only) |
| `<session_id>_unlabeled.csv` | `start,end,cut_state` of spans whose label is `-1` and whose cut state is `cutting` (abstained) or `unknown` (no rule, or the source does not say); written only when such spans exist; build_db stores cut_state as given with label -1, never as non-cutting, and rejects rows that are not finite, have start >= end, end beyond the audio, or overlap label.csv / each other |
| `_manifest/dataset_index.csv` | `dataset,session_id,source_relative_path`: which source file became which `dataset<N>` (manifest only) |
| Tables | `sources`, `sessions`, `events`, `context` (analysis) and `provenance` (separate dataset; never used as features) |

The full machine-readable spec is in [`cutsound_normalizer/spec/msm_spec.json`](cutsound_normalizer/spec/msm_spec.json).

## Label honesty rules

- Tool wear is **not** automatically an anomaly; a broken tool is **not** automatically label 1 or 2.
  If the source does not say whether the cut itself was abnormal, the label stays `-1` and a question is raised.
- Idle or machine-only sound is `non_cutting`, never `normal`.
- Output folders are `<machine_id>/dataset<N>/`. With grouping `msm_session` N is the number of the source folder `dataset<N>` (MSM identity keeps the raw numbers). With grouping `per_file` N follows sorted session id: reproducible for the same file set, renumbered when files are added or removed, so the stable key is the session id. Audio under any path component starting with `_` or `.` is skipped.
- Session ids are opaque hashes (stored in `session.json`) because source paths often encode the label (e.g. `FRESH520/RPM520A0001.wav`);
  the original path is kept in `provenance` only. The validator fails if a native label token appears in ids or metadata.

## Tools

| Tool | What it does | Confirmation |
|---|---|---|
| `get_msm_spec`, `get_mapping_schema` | target spec and mapping JSON schema | - |
| `inspect_source` | folders, file names, audio formats, table headers, README text | - |
| `kaggle_dataset_info` | public Kaggle description, license and file list (no credentials) | - |
| `save_mapping` | schema-validates and stores the proposal for review | - |
| `convert_source` | executes an approved mapping | required |
| `validate_normalized` | checks the output against the spec | - |
| `load_tables` | parquet always; BigQuery when a project and credentials exist | required |
| BigQuery toolset | read-only queries over loaded tables (when `GOOGLE_CLOUD_PROJECT` is set) | - |

## Run

```bash
pip install -r agent/requirements.txt
```

### Propose -> approve -> normalize

A proposal is **never applied automatically**. The agent saves it with status `saved_pending_approval` and stops;
a person reads it, answers the `unresolved` questions, edits the label rules if needed, and only then runs the
offline pipeline with `--approved-by`. Commands run from the repo root.

```bash
# 1. online: the agent proposes a mapping and stops (writes $NORMALIZER_WORK_DIR/mappings/<source_id>.json)
python scripts/propose_mapping.py <source_dir> --kaggle nachiketsoni/cutting-tool-wear-audio-dataset --source-id ctwa --log propose.log
# 2. offline: score the proposal against the reference mapping, file by file (V6)
python scripts/eval_mapping.py <source_dir> work/mappings/ctwa.json agent/mappings/ctwa_reference.json --report v6_report.json
# Tool, Workpiece: case and '-'/'_'/space differences ignored; direction (UP/DOWN) and N, ap, ae, F, Z, D must match exactly; paths with a '_' or '.' component are skipped (same rule as convert)
# 3. offline: after human review, convert -> validate -> tables
python scripts/normalize.py --source <source_dir> --mapping <approved_mapping.json> --out <out_dir> --approved-by "<name>"
```

### Online mode (step 1 only)

Environment variable names (set the values yourself; never commit them):

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Gemini API key, or |
| `GOOGLE_API_KEY_FILE` | path to a text file whose first non-empty line is the key (read at runtime, never printed or written), or |
| `GOOGLE_GENAI_USE_VERTEXAI` + `GOOGLE_CLOUD_PROJECT` (+ `GOOGLE_CLOUD_LOCATION`) | Vertex AI with application-default credentials |
| `NORMALIZER_MODEL` | optional, default `gemini-3.8-flash` |
| `NORMALIZER_WORK_DIR` | optional, where proposals are saved (default `./work`) |

`propose_mapping.py` prints the agent's tool sequence, token usage and final reply (optionally to `--log`). It exits 0
only when a schema-valid proposal was saved. With no credentials it prints `NOT RUN` and exits 3; an API error exits 1;
no valid proposal exits 2. A failed run never creates or overwrites the proposal file (the agent saves into a temporary
folder that is copied to the work folder only on success). Without `GOOGLE_CLOUD_PROJECT`, `load_tables` reports
BigQuery as skipped.

Interactive: `adk web agent`. Known issue: the Gemini API rejects the `$ref`/`$defs` in the schema returned by
`get_mapping_schema` (400 INVALID_ARGUMENT). `propose_mapping.py` serves the same schema with references inlined;
the interactive agent needs the same fix in `agent.py` before it can run.

### Offline mode (steps 2 and 3)

No Gemini call and no key. `normalize.py` executes an approved mapping (e.g. `agent/mappings/ctwa_reference.json`);
if the mapping still has unresolved questions, conversion is refused without `--approved-by`.

## Data and licenses

No audio or data is stored in this repository. Download each dataset from its source:

- MSM: Kim et al., *Scientific Data* 13:945 (2026), doi:10.1038/s41597-026-07255-7; data CC BY 4.0, Kaggle doi:10.34740/kaggle/ds/8392825.
- Cutting Tool Wear Audio Dataset: N. Soni et al., IEEE R10-HTC 2023, doi:10.1109/R10-HTC57504.2023.10461855; Apache 2.0.
