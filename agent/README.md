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
| `audio_profile` | band energy (0-2k ... 16-20k Hz), RMS, clipping, on the 0.1 s grid (phase 2) | - |
| `detect_change_points` | times where band energy changes persistently by >= 6 dB (0.5 s and 2 s scales) | - |
| `estimate_spindle_speed` | rpm candidates from rotation-locked lines, compared with a claimed speed (never steered by it) | - |
| `class_separability` | per-band Mann-Whitney AUC between two folders / classes | - |
| `render_session_figure` | viewer-style PNG: waveform + 0-4k / 1-4k / 4-10k / 10-24k dB, candidate times marked | - |
| `save_proposals` | schema-validates the evidence proposal file, rejects forbidden keys, checks evidence numbers | - |

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

## Evidence run (phase 2): listen, propose, never apply

The agent declares a **check plan** per folder from the claimed conditions (N, Z, D, material, coolant) and the folder
type, runs the acoustic tools on a bounded sample (at most 8 files per folder; windows of at most 60 s on long files),
and writes `<NORMALIZER_WORK_DIR>/proposals/<source_id>.json`. It never writes `label.csv` and never changes N.
Allowed conclusions: "the sound state changes here", "the spindle speed does not match the claim", "these two classes
are / are not acoustically separable". The tools' known limits from the pre-registered MSM evaluation are part of the
instruction (e.g. change-point precision is unreliable in long industrial sessions with unlabeled gaps; >= 6 dB steps
are needed, so gentle passes give no point; rpm estimates at >= 9,000 rpm can lock onto a sub-synchronous bearing comb,
so `harmonic_ambiguity` and `subsynchronous_of` are always reported; with Z unknown an rpm mismatch is a hypothesis).

| Folder type | Default checks |
|---|---|
| BASE (idle) | `audio_profile`, `class_separability` against the cutting folder at the same rpm (no cutting signature?) |
| BROKEN | `detect_change_points` on the first 6 files, `estimate_spindle_speed` on 2 early + 2 later files, a figure per change point |
| FRESH / MODERATE | `class_separability` FRESH vs MODERATE, `estimate_spindle_speed` on 1-2 files each |
| MSM session (has `label.csv`) | consistency only: change points vs label start/end, rpm vs label N; no label candidates |

Proposal file (`cutsound_normalizer/proposals.py`, `ProposalFile`): `source_id`, `mode` (`label_candidates` |
`consistency_only`), `conditions`, `check_plan` [{folder, folder_type, tools, files, sampling, reason}], `proposals`
[{id, folder, source_relative_path, kind (change_point | rpm_mismatch | class_separability | question), start_s,
end_s, candidate_label (change points only), confidence 0-1, evidence [{"tool": ..., numbers copied from the tool}],
rationale, question, figure, evidence_check}], `consistency` (MSM), `errors` (files whose tool call failed),
`usage_limits_applied`, `summary`, `status: pending_approval`.

```bash
python scripts/propose_evidence.py <source_dir> --mapping agent/mappings/ctwa_reference.json --source-id ctwa --log evidence.log
python scripts/eval_agent_phase2.py work/proposals/ctwa.json            # FEA-001 / FEA-003 (frozen checker), GOLD onset
python scripts/apply_proposals.py work/proposals/ctwa.json --mapping agent/mappings/ctwa_reference.json        --out work/mappings/ctwa_with_evidence.json --approvals approvals.json [--replace-existing]
python scripts/normalize.py --source <source_dir> --mapping work/mappings/ctwa_with_evidence.json --out <out> --approved-by "<name>"
```

Approval list: `{"approved_by": "name", "approve": [{"id": "p024", "msm_label": 1, "before_label": 2,
"anomaly_type": "tool_breakage_onset"}, {"id": "p010"}]}`. An approved change point with a 0/1/2 label becomes an
`interval_label` (`end_s` null = to the end of the file; `before_label` also labels `[0, start_s)`); everything else
becomes an `unresolved` question. The base mapping is never modified.

Approval flags:

| Flags | Who approves | What is written |
|---|---|---|
| `--approvals <file>` | the person named in `approved_by` (required; must be non-empty and must not contain "test") | each approved interval with confidence `expert_rule` |
| `--approve-all --unsafe-test-approval` | nobody (tests only) | every interval with confidence `test_only`; the mapping and each entry are tagged `approved_by "TEST (not a human)"`. `test_only` is not a mapping-schema value, so `normalize.py` refuses such a mapping. |
| `--approve-all` alone | | refused (exit 2) |

The labels in `datasets/normalized_v4` are a phase-2 **test artefact**, not owner-approved. In particular, the
C0005 onset was moved from 1.0 to 1.5 s by a GA acceptance-test approval with `--replace-existing`. Do not treat v4
as canonical; v3 and the owner's reviewed intervals remain the reference.

`propose_evidence.py` exits 3 without credentials, 1 on a model/runtime error or a forbidden call (`convert_source`,
`load_tables`, `save_mapping`), 2 when no valid proposal file was saved; on failure nothing is written to the work
folder. Every agent write of the run goes to a staging folder that is discarded on failure: `save_proposals`,
`render_session_figure`, and even a forbidden `save_mapping`. A file whose tool call fails is listed in `errors` and
the run continues.

`apply_proposals.py` checks the proposal file and the approval list at every nesting level, evidence items
included. It rejects any of the following, names the entry and path (e.g. `p024.evidence[0]: forbidden key
'convert_source'`), exits 4 and writes nothing:
- a key `set_N`, `new_N`, `N_override`, `write_label_csv`, `label_csv`, `apply`, `overwrite`, `N`, `msm_label`,
  `convert_source`, `load_tables` or `save_mapping`;
- a key naming `label.csv`;
- a string value equal to a write action, e.g. `{"action": "write_label_csv"}`;
- a write target pointing at `label.csv`.

Inside `conditions` and `label_ref`, `N` and `msm_label` only quote claims, so they are allowed there.

`save_proposals` checks evidence per entry (`evidence_check`). Every evidence item must reproduce ONE logged call of
the cited tool:
- on the entry's own file, with no fallback to other calls;
- in an overlapping window;
- with the same arguments (`claimed_N`, `flutes`, `t0_s`/`t1_s`);
- with every number equal to the value under the same field within max(0.5 %, 0.01), as a faithful rounding;
- with one change point's time, band index and dB coming from the same record.

`save_proposals` also prefixes the summary with counts it computes itself (`[computed by save_proposals: …]`, also in
`counts`). Use `--print-prompt` to see the conditions message without calling the model.

The message gives each folder's file count and 3 example names, not the full listing. Tool results go back to the
model without `params` and with at most 5 rpm candidates. The run log and the evidence check keep the full result,
and the tool declarations are unchanged.

**Honest consistency reporting (MSM / labelled sessions).**
- In every change-point window, every label.csv boundary at least 2 s from the window edges is checked. Each checked
  boundary is recorded as a consistency item, hit (a point within 0.5 s) or miss.
- Each window gets a `boundary_windows` entry: checked / matched / missed / points off every boundary.
- A window with any miss cannot be `consistent` (the schema refuses it). The summary may not say the points "all
  align".
- `save_proposals` recomputes these counts from the tool output into `boundary_audit` and warns when the agent's
  counts differ.
- Example, imi_vmx30ui/dataset14 (GH run): 27 checked / 18 matched / 9 missed. All 9 misses are in the 454-493 s window
  (gentle label-0 passes, < 6 dB, the documented limit).

**GOLD caveat.** GOLD-style spot checks (e.g. the RPM520C0005 onset at 1.5 s) confirm that the tools work. They are
not discovery tests. The agent was told to scan the first 6 BROKEN files, so a GOLD PASS does not show the agent's
judgment.

