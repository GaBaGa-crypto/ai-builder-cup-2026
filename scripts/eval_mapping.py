"""Compare a proposed mapping with a reference mapping by EFFECT on the real files (V6).

Both mappings are applied to every audio path of the source; agreement is counted per path for the label,
cut_state and each MSM condition field. Regex wording may differ; only the resulting values matter.
Tool and Workpiece compare loosely (free text); direction (enum UP/DOWN) and the numeric fields compare exactly.
Audio paths with any component starting with '_' or '.' are skipped, the same rule as convert_source.

Usage:
  python scripts/eval_mapping.py <source_dir> <proposed.json> <reference.json> [--report out.json]
"""
import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from cutsound_normalizer.mapping_schema import Mapping  # noqa: E402
from cutsound_normalizer.tools.convert import _field_values, _skipped  # noqa: E402

FIELDS = ["N", "ap", "ae", "F", "Z", "D", "Tool", "Workpiece", "direction"]
TEXT_FIELDS = ("Tool", "Workpiece")  # loose comparison only here; direction (enum UP/DOWN) and numeric fields exact


def _norm(v) -> str:
    """Compare free-text fields loosely: case, surrounding spaces and '-'/'_' vs ' ' do not count as disagreement."""
    return re.sub(r"[\s_\-]+", " ", str(v)).strip().lower()


def resolve(m: Mapping, rel: str) -> dict:
    token = None
    if m.label_pattern:
        mm = re.search(m.label_pattern, rel)
        token = mm.groupdict().get("v") if mm else None
    rule = {r.native: r for r in m.label_rules}.get(token)
    out = {"label": rule.msm_label if rule else -1, "cut_state": rule.cut_state if rule else "unknown"}
    for k, v in _field_values(m, rel).items():
        out[k] = _norm(v) if k in TEXT_FIELDS else v  # direction, N/ap/ae/F/Z/D: exact value ('' if unknown)
    return out


ap = argparse.ArgumentParser()
ap.add_argument("source_dir")
ap.add_argument("proposed")
ap.add_argument("reference")
ap.add_argument("--report", default=None)
a = ap.parse_args()

prop = Mapping.model_validate_json(Path(a.proposed).read_text(encoding="utf-8"))
ref = Mapping.model_validate_json(Path(a.reference).read_text(encoding="utf-8"))
root = Path(a.source_dir)
paths = sorted(rel for f in glob.glob(str(root / ref.session_rule.audio_glob), recursive=True)
               for rel in [Path(f).relative_to(root).as_posix()] if not _skipped(rel))  # same files convert converts
prop_paths = {rel for f in glob.glob(str(root / prop.session_rule.audio_glob), recursive=True)
              for rel in [Path(f).relative_to(root).as_posix()] if not _skipped(rel)}

agree, disagree = Counter(), {}
label_abstained = label_wrong = 0  # -1 where the reference decided (honest abstention) vs a definite label that differs
for rel in paths:
    r, p = resolve(ref, rel), resolve(prop, rel)
    if r["label"] != p["label"]:
        if p["label"] == -1:
            label_abstained += 1
        else:
            label_wrong += 1
    for k in ["label", "cut_state", *FIELDS]:
        if r[k] == p[k]:
            agree[k] += 1
        else:
            disagree.setdefault(k, []).append({"path": rel, "reference": r[k], "proposed": p[k]})

n = len(paths)
report = {
    "n_paths": n,
    "audio_selection_recall": round(len(prop_paths & set(paths)) / n, 4) if n else None,
    "extra_paths_selected": len(prop_paths - set(paths)),
    "agreement": {k: round(agree[k] / n, 4) if n else None for k in ["label", "cut_state", *FIELDS]},
    "comparison": {"text_fields_loose": list(TEXT_FIELDS),
                   "text_rule": "case, surrounding spaces and '-'/'_'/space differences ignored",
                   "numeric_fields_exact": [f for f in FIELDS if f not in TEXT_FIELDS and f != "direction"],
                   "enum_fields_exact": ["direction"],
                   "skipped_paths": "any path component starting with '_' or '.' (same rule as convert_source)"},
    "label_split": {"agree": agree["label"], "abstained_minus1": label_abstained, "wrong_definite": label_wrong,
                    "note": "abstained = proposal says -1 where the reference holds a human decision; wrong = a 0/1/2 that differs"},
    "label_rules": {"reference": {r.native: r.msm_label for r in ref.label_rules},
                    "proposed": {r.native: r.msm_label for r in prop.label_rules}},
    "audio_rule_match": prop.audio.channel == ref.audio.channel and prop.audio.gain == ref.audio.gain,
    "proposed_unresolved": prop.unresolved,
    "disagreement_examples": {k: v[:3] for k, v in disagree.items()},
}
print(json.dumps(report, ensure_ascii=False, indent=1))
if a.report:
    Path(a.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
