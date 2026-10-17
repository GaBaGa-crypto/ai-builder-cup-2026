"""Score an agent proposal file with the pre-registered agent-level rules (offline, no LLM).

label_candidates files (e.g. CTWA):
  * schema + FEA-013 key guard (cutsound_normalizer.proposals);
  * FEA-001 and FEA-003 by running the FROZEN checker harness/agent1-phase2/tdd/check_scenarios.py unchanged
    (the file is split into its BASE and BROKEN folder entries for FEA-001);
  * GOLD (TEST_PLAN 1.5): a change point cited for BROKEN520/RPM520C0005 with t in [0.75, 1.80] s - reported as is.
consistency_only files (MSM sessions with label.csv):
  * no candidate_label anywhere;
  * every change-point time cited in proposals/consistency evidence vs the nearest label.csv boundary (|dt|).

Usage:
  python scripts/eval_agent_phase2.py <proposals.json> [--checker <check_scenarios.py>] [--label-csv <label.csv>]
                                      [--report out.json]
Exit 0 when every applicable rule passes, 1 otherwise, 2 unreadable / invalid file.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "agent"))
from cutsound_normalizer.proposals import ProposalFile, find_forbidden_keys  # noqa: E402

DEFAULT_CHECKER = REPO.parents[0] / "harness" / "agent1-phase2" / "tdd" / "check_scenarios.py"
GOLD_FILE = re.compile(r"BROKEN520/RPM520C0005", re.IGNORECASE)
GOLD_WINDOW = (0.75, 1.80)
FOLDER_TYPES = ("BASE", "BROKEN", "FRESH", "MODERATE")


def folder_of(e: dict) -> str:
    if e.get("folder"):
        return str(e["folder"])
    rel = str(e.get("source_relative_path") or "")
    return rel if not Path(rel).suffix else str(Path(rel).parent.as_posix())


def folder_type(folder: str):
    name = Path(folder).name.upper()
    for t in FOLDER_TYPES:
        if name.startswith(t):
            return t
    return None


def cited_times(entries, tool="detect_change_points"):
    """(path, t_s) for every change-point evidence item; falls back to start_s of change_point entries."""
    out = []
    for e in entries:
        ts = [ev.get("t_s") for ev in e.get("evidence") or [] if ev.get("tool") == tool
              and isinstance(ev.get("t_s"), (int, float))]
        for ev in e.get("evidence") or []:
            if ev.get("tool") == tool and isinstance(ev.get("change_points_t_s"), list):
                ts += [t for t in ev["change_points_t_s"] if isinstance(t, (int, float))]
        if not ts and e.get("kind") == "change_point" and isinstance(e.get("start_s"), (int, float)):
            ts = [e["start_s"]]
        out += [(e.get("source_relative_path"), float(t), e) for t in ts]
    return out


def run_checker(checker: Path, args) -> dict:
    p = subprocess.run([sys.executable, "-X", "utf8", str(checker), *map(str, args)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return {"exit": p.returncode, "output": (p.stdout + p.stderr).strip()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("proposals")
    ap.add_argument("--checker", default=str(DEFAULT_CHECKER))
    ap.add_argument("--label-csv", default=None)
    ap.add_argument("--report", default=None)
    a = ap.parse_args()
    try:
        raw = json.loads(Path(a.proposals).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"UNREADABLE: {e}")
        return 2
    rep = {"file": a.proposals, "rules": {}}
    bad = find_forbidden_keys(raw)
    rep["rules"]["FEA-013_no_forbidden_keys"] = {"pass": not bad, "violations": bad}
    try:
        pf = ProposalFile.model_validate(raw)
        rep["rules"]["schema"] = {"pass": True}
    except Exception as e:  # noqa: BLE001
        rep["rules"]["schema"] = {"pass": False, "error": str(e)[:1500]}
        pf = None
    entries = raw.get("proposals") or []
    print(f"file: {a.proposals}  mode={raw.get('mode')}  proposals={len(entries)}  "
          f"consistency={len(raw.get('consistency') or [])}  errors={len(raw.get('errors') or [])}")
    print(f"[schema] {'PASS' if rep['rules']['schema']['pass'] else 'FAIL ' + rep['rules']['schema'].get('error', '')[:300]}")
    print(f"[FEA-013 keys] {'PASS' if not bad else 'FAIL ' + str(bad)}")

    print("[check plan]")
    for c in raw.get("check_plan") or []:
        print(f"   {c.get('folder')} | {c.get('folder_type')} | {', '.join(c.get('tools') or [])} | "
              f"{len(c.get('files') or [])} files | {str(c.get('reason'))[:140]}")
    tools_by_folder = {}
    for e in entries:
        tools_by_folder.setdefault(folder_of(e), set()).update(ev.get("tool") for ev in e.get("evidence") or [])
    print("[tools cited per folder] " + "; ".join(f"{k}: {sorted(v)}" for k, v in sorted(tools_by_folder.items())))
    rep["tools_by_folder"] = {k: sorted(v) for k, v in tools_by_folder.items()}

    if raw.get("mode") == "consistency_only":
        labelled = [e.get("id") for e in entries if e.get("candidate_label") is not None]
        rep["rules"]["MSM_no_candidate_label"] = {"pass": not labelled, "entries_with_label": labelled}
        print(f"[MSM no candidate_label] {'PASS' if not labelled else 'FAIL ' + str(labelled)}")
        lc = Path(a.label_csv) if a.label_csv else Path(raw.get("source_dir") or ".") / "label.csv"
        import csv
        with open(lc, encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        bounds = sorted({(float(r["start"]), "start") for r in rows} | {(float(r["end"]), "end") for r in rows})
        cit = cited_times(entries + list(raw.get("consistency") or []))
        matches = []
        for path, t, _e in cit:
            b, kind = min(bounds, key=lambda x: abs(x[0] - t))
            matches.append({"t_s": t, "boundary_s": b, "boundary": kind, "abs_dt_s": round(abs(b - t), 3)})
        uniq = {m_["t_s"]: m_ for m_ in matches}
        ms = sorted(uniq.values(), key=lambda m_: m_["t_s"])
        n05 = sum(1 for m_ in ms if m_["abs_dt_s"] <= 0.5)
        rep["MSM_change_points_vs_label"] = {"label_csv": str(lc), "n_cited_times": len(ms), "n_within_0p5s": n05,
                                             "examples": ms[:12]}
        print(f"[MSM change points vs label.csv] cited times={len(ms)} within 0.5 s of a boundary={n05}")
        for m_ in ms[:12]:
            print(f"   t={m_['t_s']:.2f} s  nearest {m_['boundary']} {m_['boundary_s']:.3f} s  |dt|={m_['abs_dt_s']:.3f} s")
        rep["rules"]["MSM_cites_matching_change_points"] = {"pass": n05 >= 3, "n_within_0p5s": n05}
    else:
        checker = Path(a.checker)
        groups = {"BASE": [], "BROKEN": []}
        for e in entries:
            t = folder_type(folder_of(e))
            if t in groups:
                groups[t].append(e)
        with tempfile.TemporaryDirectory(prefix="evalp2_") as tmp:
            fb, fk = Path(tmp) / "BASE.json", Path(tmp) / "BROKEN.json"
            fb.write_text(json.dumps({"proposals": groups["BASE"]}), encoding="utf-8")
            fk.write_text(json.dumps({"proposals": groups["BROKEN"]}), encoding="utf-8")
            r1 = run_checker(checker, ["fea001", fb, fk])
            r3 = run_checker(checker, ["fea003", a.proposals])
        rep["rules"]["FEA-001"] = {"pass": r1["exit"] == 0, **r1, "n_base": len(groups["BASE"]),
                                   "n_broken": len(groups["BROKEN"])}
        rep["rules"]["FEA-003"] = {"pass": r3["exit"] == 0, **r3}
        print(f"[FEA-001] check_scenarios.py exit {r1['exit']} (BASE entries {len(groups['BASE'])}, BROKEN {len(groups['BROKEN'])})")
        print("   " + r1["output"].replace("\n", "\n   "))
        print(f"[FEA-003] check_scenarios.py exit {r3['exit']}")
        print("   " + r3["output"].replace("\n", "\n   "))
        gold = [(p_, t, e) for p_, t, e in cited_times(entries) if GOLD_FILE.search(str(p_))]
        hits = [t for _, t, _ in gold if GOLD_WINDOW[0] <= t <= GOLD_WINDOW[1]]
        rep["rules"]["GOLD"] = {"pass": bool(hits), "window_s": list(GOLD_WINDOW), "cited_times_s": [t for _, t, _ in gold],
                                "in_window": hits,
                                "entries": [{"id": e.get("id"), "start_s": e.get("start_s"),
                                             "candidate_label": e.get("candidate_label"), "confidence": e.get("confidence")}
                                            for _, _, e in gold]}
        print(f"[GOLD RPM520C0005 onset in {list(GOLD_WINDOW)} s] {'PASS' if hits else 'NOT FOUND'} "
              f"cited times={[t for _, t, _ in gold]} in window={hits}")
    ok = all(r.get("pass") for r in rep["rules"].values())
    rep["pass"] = ok
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"== overall: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
