"""Approved evidence proposals -> mapping interval_labels / unresolved questions (offline, no LLM).

The agent's proposal file is never applied by the agent. A person writes an approval list naming the entries they
accept (and, for a change point, the label they decide); this script copies them into a NEW mapping file, which then
goes through scripts/normalize.py like any approved mapping. N and the other conditions are never touched here.

Usage:
  python scripts/apply_proposals.py <proposals.json> --mapping <base_mapping.json> --out <new_mapping.json>
                                    (--approvals <approvals.json> | --approve-all --unsafe-test-approval)
                                    [--replace-existing]

approvals.json (the only human path):
  {"approved_by": "name of the person",
   "approve": [{"id": "p003", "msm_label": 1, "start_s": 1.5, "end_s": null, "before_label": 2,
                "cut_state": "cutting", "anomaly_type": "tool_breakage_onset", "note": "listened 2026-10-17"},
               {"id": "p007"}]}
  - change_point + a label 0/1/2 (from the approval, else the proposal's candidate_label) -> interval_label
    [start_s, end_s) on that file (end_s null = to the end of the file); before_label adds [0, start_s) as well.
  - anything else (rpm questions, separability, change points without a 0/1/2 label) -> mapping 'unresolved'.
  - approved_by must be a person: non-empty and not containing "test"; every interval gets confidence "expert_rule".
--approve-all: TESTS ONLY, and only together with --unsafe-test-approval (otherwise exit 2). Every proposal is
  approved with its own candidate_label, every new interval gets confidence "test_only" and the mapping and each
  entry are tagged approved_by "TEST (not a human)". "test_only" is not a mapping-schema confidence, so the written
  mapping is refused by normalize/convert: a test approval can never become a dataset label.
--replace-existing: drop the base mapping's interval_labels on the files that receive approved intervals.

Refusals (nothing is written): a forbidden key or action anywhere in the proposal file or the approval list, at any
nesting level including evidence items (FEA-013: convert_source / load_tables / save_mapping, set_N / new_N / N,
write_label_csv / label_csv or a label.csv write; exit 4, the entry is named); invalid proposal file or unknown ids
(exit 2); --approve-all without --unsafe-test-approval, or an approval list without a human approved_by (exit 2);
a label approval on a consistency_only file (the source is already labelled, exit 2).
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
from cutsound_normalizer.mapping_schema import IntervalLabel, Mapping  # noqa: E402
from cutsound_normalizer.proposals import FEA013_MESSAGE, ProposalFile, find_forbidden_keys  # noqa: E402

APPROVAL_KEYS = {"id", "msm_label", "start_s", "end_s", "before_label", "cut_state", "anomaly_type", "note"}
TEST_APPROVER = "TEST (not a human)"
TEST_CONFIDENCE = "test_only"


def fail(code: int, msg: str, items=()):
    print(msg)
    for it in items:
        print(f"  - {it}")
    print("Nothing was written.")
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("proposals")
    ap.add_argument("--mapping", required=True, help="base mapping JSON (not modified)")
    ap.add_argument("--out", required=True, help="new mapping JSON to write")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--approvals", help="approval list JSON written by a person")
    g.add_argument("--approve-all", action="store_true",
                   help="TESTS ONLY (needs --unsafe-test-approval): approve every entry as proposed, tagged test_only")
    ap.add_argument("--unsafe-test-approval", action="store_true",
                    help="required with --approve-all: acknowledges that no human approved anything")
    ap.add_argument("--replace-existing", action="store_true")
    a = ap.parse_args()

    if a.approve_all and not a.unsafe_test_approval:
        fail(2, "REFUSED: --approve-all approves the agent's own labels with no human involved. It is for tests only "
                "and needs --unsafe-test-approval; the entries are then tagged confidence 'test_only' and approved_by "
                f"'{TEST_APPROVER}'. A real approval is an --approvals file written and signed by a person.")
    if a.unsafe_test_approval and not a.approve_all:
        fail(2, "REFUSED: --unsafe-test-approval is only meaningful with --approve-all.")

    if Path(a.out).resolve() == Path(a.mapping).resolve():
        fail(2, "REFUSED: --out must differ from --mapping (the base mapping is never modified in place).")
    raw = json.loads(Path(a.proposals).read_text(encoding="utf-8"))
    bad = find_forbidden_keys(raw)            # every nesting level, evidence items included; entries named
    if bad:
        fail(4, f"REJECTED (FEA-013) {a.proposals}: {FEA013_MESSAGE}", bad)
    try:
        pf = ProposalFile.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        fail(2, f"REFUSED: invalid proposal file: {str(e)[:1500]}")
    by_id = {p.id: p for p in pf.proposals}

    if a.approve_all:
        approved_by, confidence = TEST_APPROVER, TEST_CONFIDENCE
        items = [{"id": p.id} for p in pf.proposals]
    else:
        appr = json.loads(Path(a.approvals).read_text(encoding="utf-8"))
        viol = find_forbidden_keys(appr, allow=("msm_label",))   # an approval may set msm_label on an interval
        if viol:
            fail(4, f"REJECTED (FEA-013) {a.approvals}: an approval may set a label on an interval, never N or label.csv",
                 viol)
        approved_by, confidence = str(appr.get("approved_by") or "").strip(), "expert_rule"
        if not approved_by:
            fail(2, "REFUSED: the approval list has no approved_by (the name of the person who approves).")
        if "test" in approved_by.lower():
            fail(2, f"REFUSED: approved_by {approved_by!r} names a test, not a person. Test approvals go through "
                    "--approve-all --unsafe-test-approval and are tagged test_only.")
        items = appr.get("approve") or []
        unknown = [f"approve[{i}]: unknown key {k!r}" for i, it in enumerate(items) for k in it if k not in APPROVAL_KEYS]
        if unknown:
            fail(2, "REFUSED: approval list has unknown keys", unknown)
    missing = [it.get("id") for it in items if it.get("id") not in by_id]
    if missing:
        fail(2, f"REFUSED: approval ids not in the proposal file: {missing}")

    m = Mapping.model_validate_json(Path(a.mapping).read_text(encoding="utf-8"))
    new_intervals, new_questions, log = [], [], []
    for it in items:
        p = by_id[it["id"]]
        label = it.get("msm_label", p.candidate_label)
        ref = f"evidence {pf.source_id}/{p.id}"
        ev_txt = "; ".join(
            f"{e.get('tool')} " + ", ".join(f"{k}={e[k]}" for k in ("t_s", "max_abs_delta_db", "best_rpm", "best_auc")
                                            if k in e)
            for e in p.evidence)[:300]
        if p.kind == "change_point" and label in (0, 1, 2):
            if pf.mode == "consistency_only":
                fail(2, f"REFUSED: {p.id}: a label approval on a consistency_only proposal file (source already labelled).")
            start = it.get("start_s", p.start_s)
            if start is None:
                ts = [e.get("t_s") for e in p.evidence if isinstance(e.get("t_s"), (int, float))]
                start = ts[0] if ts else None
            if start is None:
                fail(2, f"REFUSED: {p.id}: change point without a time (give start_s in the approval).")
            end = it.get("end_s", p.end_s)
            evidence = (f"{ref} approved by {approved_by} {dt.date.today().isoformat()}"
                        + (f" ({it['note']})" if it.get("note") else "") + f": {ev_txt}. Agent rationale: {p.rationale[:200]}")
            cut_state = it.get("cut_state", "cutting")
            if it.get("before_label") is not None:
                new_intervals.append(IntervalLabel(path=p.source_relative_path, start_s=0.0, end_s=float(start),
                                                   msm_label=it["before_label"], cut_state=cut_state,
                                                   anomaly_type=None, confidence="expert_rule",
                                                   evidence=evidence + " [span before the approved change point]"))
            new_intervals.append(IntervalLabel(path=p.source_relative_path, start_s=float(start),
                                               end_s=None if end is None else float(end), msm_label=label,
                                               cut_state=cut_state, anomaly_type=it.get("anomaly_type"),
                                               confidence="expert_rule", evidence=evidence))
            log.append(f"{p.id} -> interval_label {p.source_relative_path} [{start}, {end if end is not None else 'end'}) label {label}"
                       + (f" (+ [0, {start}) label {it['before_label']})" if it.get("before_label") is not None else ""))
        else:
            q = p.question or p.rationale
            new_questions.append(f"[{ref}, {p.kind}, {p.source_relative_path}, confidence {p.confidence}] {q} "
                                 f"(evidence: {ev_txt})")
            log.append(f"{p.id} -> unresolved ({p.kind})")

    if a.replace_existing:
        paths = {il.path for il in new_intervals}
        dropped = [il for il in m.interval_labels if il.path in paths]
        m.interval_labels = [il for il in m.interval_labels if il.path not in paths]
        if dropped:
            log.append(f"--replace-existing: dropped {len(dropped)} base interval_labels on {sorted(paths)}")
    n_base, n_base_q = len(m.interval_labels), len(m.unresolved)
    m.interval_labels.extend(new_intervals)
    m.unresolved.extend(new_questions)
    out_d = json.loads(m.model_dump_json())
    for il in out_d["interval_labels"][n_base:]:       # the intervals approved now
        il["confidence"] = confidence
    if a.approve_all:                                  # not a schema value: normalize/convert refuse this mapping
        out_d["approved_by"] = TEST_APPROVER
        out_d["unresolved"][n_base_q:] = [f"[{TEST_CONFIDENCE}, approved_by {TEST_APPROVER}] " + q
                                          for q in out_d["unresolved"][n_base_q:]]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out_d, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"approved_by={approved_by} confidence={confidence} proposals={a.proposals} base={a.mapping}")
    for line in log:
        print(f"  {line}")
    print(f"wrote {a.out}: interval_labels {len(m.interval_labels)} (+{len(new_intervals)}), "
          f"unresolved {len(m.unresolved)} (+{len(new_questions)}); N and other conditions unchanged")


if __name__ == "__main__":
    main()
