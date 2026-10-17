"""Evidence proposals (phase 2): the only thing the agent writes during an evidence run.

A proposal file holds the agent's CHECK PLAN, its candidates (change points, rpm questions, class separability)
with numeric evidence copied from the deterministic acoustic tools, a confidence and the questions for a human.
It never writes label.csv and never changes N: entries carrying such keys are rejected by name (FEA-013), here and
again in scripts/apply_proposals.py. Contract: harness/agent1-phase2/scenarios.md (interface contract).

This module also wraps the four acoustic tools for the agent: every call is logged (tool, arguments, result) so
that save_proposals can check that each number in an entry's evidence really came out of a tool call, and tool
errors are collected so a failing file is recorded in the proposal file instead of stopping the run (FEA-012).
"""
from __future__ import annotations

import datetime as dt
import functools
import inspect
import json
import math
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ACOUSTIC_TOOLS = ("audio_profile", "detect_change_points", "estimate_spindle_speed", "class_separability")
PLAN_TOOLS = ACOUSTIC_TOOLS + ("render_session_figure",)
KINDS = ("change_point", "rpm_mismatch", "class_separability", "question")
# FEA-013: an agent may not write label.csv or change N. The first seven are the pre-registered list
# (tdd/check_scenarios.FORBIDDEN_KEYS); 'N' and 'msm_label' are added because they would also be direct edits, and
# the three phase-1 write tools (convert_source, load_tables, save_mapping) because naming them is a request to act.
FORBIDDEN_KEYS = ("new_N", "set_N", "N_override", "write_label_csv", "label_csv", "apply", "overwrite",
                  "N", "msm_label", "convert_source", "load_tables", "save_mapping")
# a string VALUE equal to one of these (case-insensitive) is a forbidden action request, e.g. {"action": "set_N"}
FORBIDDEN_ACTIONS = ("convert_source", "load_tables", "save_mapping", "write_label_csv", "set_N", "new_N",
                     "N_override")
# under these keys a string value that names label.csv is a request to write it (e.g. {"write_to": ".../label.csv"})
_WRITE_KEYS = ("action", "actions", "do", "op", "operation", "command", "cmd", "call", "tool_call", "write", "write_to",
               "writes", "output", "out", "target", "dest", "destination", "save_to", "save_as", "file_to_write")
_QUOTING_KEYS = ("conditions", "label_ref")     # subtrees that quote claims / existing label rows
_QUOTED_OK = ("N", "msm_label")
MATCH_REL_TOL, MATCH_ABS_TOL = 0.005, 0.01     # evidence number vs tool value: within max(0.5 %, 0.01)
BOUNDARY_TOL_S = 0.5                           # change point vs label.csv boundary (pre-registered tdd tolerance)

WORK_DIR: Optional[Path] = None      # set by the headless runner; default $NORMALIZER_WORK_DIR or ./work
SOURCE_ROOT: Optional[Path] = None   # relative wav paths given by the model are resolved against this folder
MODEL_NAME: Optional[str] = None
# label.csv boundaries per airborne file (normalised absolute path -> sorted start/end times), set by the headless
# runner for consistency_only sources; save_proposals then audits every logged detect_change_points window.
LABEL_BOUNDARIES: Dict[str, List[float]] = {}

_TOOL_LOG: List[Dict[str, Any]] = []     # every acoustic tool call of this process (bounded)
_TOOL_ERRORS: List[Dict[str, Any]] = []
_LOG_MAX = 2000


def work_dir() -> Path:
    return WORK_DIR if WORK_DIR is not None else Path(os.environ.get("NORMALIZER_WORK_DIR", "work"))


def reset_tool_log() -> None:
    _TOOL_LOG.clear()
    _TOOL_ERRORS.clear()


def tool_log() -> List[Dict[str, Any]]:
    return list(_TOOL_LOG)


def tool_errors() -> List[Dict[str, Any]]:
    return list(_TOOL_ERRORS)


def set_label_boundaries(per_file: Dict[str, List[float]]) -> None:
    """{wav path: [label.csv start/end times]} for the boundary audit of a consistency_only run."""
    LABEL_BOUNDARIES.clear()
    for p, b in per_file.items():
        LABEL_BOUNDARIES[_norm_path(p)] = sorted({round(float(x), 6) for x in b})


def route_writes_to(staging: Path) -> None:
    """Point EVERY agent write of an evidence run at the staging folder: the proposal file, the figures and the
    phase-1 save_mapping (forbidden during an evidence run, but if the model calls it anyway its file must land in
    staging, which is discarded when the run fails)."""
    global WORK_DIR
    staging = Path(staging)
    WORK_DIR = staging
    from .tools import figure as _figure
    _figure.FIG_DIR = staging / "figures"
    from . import agent as _agent            # imported lazily: agent.py imports this module
    _agent.WORK_DIR = staging


# ------------------------------------------------------------------------------------------- schema
def _validate_evidence(ev):
    for i, e in enumerate(ev):
        if e.get("tool") not in ACOUSTIC_TOOLS:
            raise ValueError(f"evidence[{i}].tool must be one of {list(ACOUSTIC_TOOLS)}, got {e.get('tool')!r}")
        if not _numbers({k: v for k, v in e.items() if k != "tool"}):
            raise ValueError(f"evidence[{i}] ({e.get('tool')}) carries no number copied from the tool output")
    return ev


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    tool: str
    error: str


class CheckPlanItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(description="Folder (or MSM session) the plan line is about, relative to the source root")
    folder_type: str = Field(description="What the folder is, e.g. 'BASE (machine running, no cut)', "
                                         "'BROKEN (tool broke)', 'MSM session with label.csv'")
    conditions: Dict[str, Any] = Field(default_factory=dict, description="Claimed conditions used (N, Z, D, ...)")
    tools: List[Literal[PLAN_TOOLS]] = Field(min_length=1)
    files: List[str] = Field(default_factory=list, description="Files (or path windows) actually checked")
    sampling: str = Field(description="How the files were chosen, e.g. 'first 6 of 181 + 2 spread'")
    reason: str = Field(min_length=20, description="Why these tools for this folder type")


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Optional[str] = None
    folder: Optional[str] = Field(default=None, description="Folder the entry belongs to (e.g. 'SegmentedData/BROKEN520')")
    session_id: Optional[str] = None
    source_relative_path: str
    kind: Literal[KINDS]
    start_s: Optional[float] = Field(default=None, ge=0)
    end_s: Optional[float] = Field(default=None, ge=0)
    candidate_label: Optional[Literal[0, 1, 2, -1]] = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: List[Dict[str, Any]] = Field(min_length=1)
    rationale: str = Field(min_length=20)
    question: Optional[str] = None
    figure: Optional[str] = None
    evidence_check: Optional[Dict[str, Any]] = None   # filled by save_proposals

    @field_validator("evidence")
    @classmethod
    def _evidence_items(cls, ev):
        return _validate_evidence(ev)

    @model_validator(mode="after")
    def _label_only_on_change_points(self):
        if self.candidate_label is not None and self.kind != "change_point":
            raise ValueError(f"candidate_label is only allowed on kind 'change_point' (this entry is {self.kind!r}); "
                             "rpm and separability entries are questions, not labels")
        if self.start_s is not None and self.end_s is not None and self.end_s <= self.start_s:
            raise ValueError(f"end_s ({self.end_s}) must be > start_s ({self.start_s})")
        return self


class ConsistencyItem(BaseModel):
    """A check of an existing label (MSM label.csv) against a tool result: no label is proposed."""
    model_config = ConfigDict(extra="forbid")
    source_relative_path: str
    check: Literal["change_point_vs_label_boundary", "rpm_vs_label_N", "other"]
    label_ref: Dict[str, Any] = Field(default_factory=dict, description="The label.csv row/boundary checked")
    result: Literal["consistent", "inconsistent", "ambiguous", "not_testable"]
    evidence: List[Dict[str, Any]] = Field(min_length=1)
    note: str = Field(min_length=10)
    evidence_check: Optional[Dict[str, Any]] = None

    @field_validator("evidence")
    @classmethod
    def _evidence_items(cls, ev):
        return _validate_evidence(ev)


class BoundaryWindow(BaseModel):
    """Per detect_change_points window on a labelled file: how many label.csv boundaries were checked, hit, missed."""
    model_config = ConfigDict(extra="forbid")
    source_relative_path: str
    t0_s: float
    t1_s: float
    tolerance_s: float = BOUNDARY_TOL_S
    n_checked: int = Field(ge=0, description="label.csv start/end times inside the window (>= 2 s from its edges)")
    n_matched: int = Field(ge=0, description="checked boundaries with a change point within tolerance_s")
    n_missed: int = Field(ge=0, description="checked boundaries without a change point within tolerance_s")
    n_points_off_boundary: Optional[int] = Field(default=None, ge=0,
                                                 description="change points farther than tolerance_s from every boundary")
    result: Literal["consistent", "partially_consistent", "inconsistent", "not_testable"]
    note: Optional[str] = None

    @model_validator(mode="after")
    def _counts(self):
        if self.n_matched + self.n_missed != self.n_checked:
            raise ValueError(f"boundary window {self.t0_s}-{self.t1_s}: n_matched ({self.n_matched}) + n_missed "
                             f"({self.n_missed}) must equal n_checked ({self.n_checked})")
        if self.result == "consistent" and (self.n_missed > 0 or self.n_checked == 0):
            raise ValueError(f"boundary window {self.t0_s}-{self.t1_s}: result 'consistent' is not allowed with "
                             f"{self.n_missed} missed of {self.n_checked} checked boundaries")
        return self


class ProposalFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_version: str = "0.1"
    source_id: str
    source_dir: Optional[str] = None
    mode: Literal["label_candidates", "consistency_only"] = Field(
        description="consistency_only for sources that already carry labels (MSM label.csv): no candidate_label at all")
    conditions: Dict[str, Any] = Field(default_factory=dict)
    check_plan: List[CheckPlanItem] = Field(min_length=1)
    proposals: List[Proposal] = Field(default_factory=list)
    consistency: List[ConsistencyItem] = Field(default_factory=list)
    errors: List[ToolError] = Field(default_factory=list)
    usage_limits_applied: List[str] = Field(default_factory=list)
    boundary_windows: List[BoundaryWindow] = Field(default_factory=list,
                                                   description="consistency_only: one item per change-point window")
    boundary_audit: List[Dict[str, Any]] = Field(default_factory=list,
                                                 description="computed by save_proposals from the logged tool calls")
    counts: Dict[str, Any] = Field(default_factory=dict, description="computed by save_proposals")
    summary: Optional[str] = None
    status: Literal["pending_approval"] = "pending_approval"
    created_at: Optional[str] = None
    model: Optional[str] = None

    @model_validator(mode="after")
    def _mode_rules(self):
        if self.mode == "consistency_only":
            bad = [p.id or str(i) for i, p in enumerate(self.proposals) if p.candidate_label is not None]
            if bad:
                raise ValueError(f"mode consistency_only (source already labelled) forbids candidate_label; entries {bad}")
        return self


# ------------------------------------------------------------------------------- FEA-013 key guard
def find_forbidden_keys(obj: Any, allow: tuple = ()) -> List[str]:
    """Named violations anywhere in the object (every nesting level, evidence items included).

    A key in FORBIDDEN_KEYS (the multi-letter ones case-insensitively) or a key naming label.csv is a violation; so is a
    string value equal to a FORBIDDEN_ACTIONS name, or a string value naming label.csv under a write-like key.
    Each hit names the entry: the 'id' of the nearest enclosing list item (e.g. 'p024.evidence[0]'), else its path.
    `allow` exempts key names (the approval list may carry msm_label). Inside 'conditions' and 'label_ref', which
    quote the claimed conditions / the existing label.csv row, the keys 'N' and 'msm_label' are descriptions, not
    edits; every other forbidden key and every forbidden action value is still a violation there."""
    bad_exact = {k for k in FORBIDDEN_KEYS if k not in allow}
    bad_lower = {k.lower() for k in bad_exact if len(k) > 1}
    actions = {a.lower() for a in FORBIDDEN_ACTIONS}
    hits: List[str] = []

    def walk(o: Any, where: str, key: str, quoted: bool = False) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                ks = str(k)
                kl = ks.lower()
                if quoted and ks in _QUOTED_OK:
                    pass
                elif ks not in allow and (ks in bad_exact or kl in bad_lower or "label.csv" in kl):
                    hits.append(f"{where}: forbidden key {ks!r}")
                walk(v, f"{where}.{ks}" if where != "file" else ks, ks, quoted or ks in _QUOTING_KEYS)
        elif isinstance(o, (list, tuple)):
            for i, v in enumerate(o):
                ref = v.get("id") if isinstance(v, dict) and isinstance(v.get("id"), str) and v.get("id") else None
                walk(v, ref or f"{where}[{i}]", key, quoted)
        elif isinstance(o, str):
            sv = o.strip().lower()
            if sv in actions:
                hits.append(f"{where}: forbidden action value {o.strip()!r}")
            elif key.lower() in _WRITE_KEYS and "label.csv" in sv:
                hits.append(f"{where}: forbidden write of label.csv ({key}={o.strip()[:80]!r})")

    walk(obj, "file", "")
    return hits


FEA013_MESSAGE = ("FEA-013: the agent may not write label.csv or change N/labels; a proposal is a candidate for a "
                  "human, applied only by scripts/apply_proposals.py after approval")


# ------------------------------------------------------------------------------- logged tool wrappers
def _numbers(obj) -> List[float]:
    if isinstance(obj, bool) or obj is None:
        return []
    if isinstance(obj, (int, float)):
        return [float(obj)] if math.isfinite(float(obj)) else []
    if isinstance(obj, dict):
        return [x for v in obj.values() for x in _numbers(v)]
    if isinstance(obj, (list, tuple)):
        return [x for v in obj for x in _numbers(v)]
    return []


def _round(obj, nd=3):
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return round(obj, nd) if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(v, nd) for v in obj]
    return obj


def _resolve(p: str) -> str:
    if not isinstance(p, str):
        return p
    q = Path(p)
    if q.is_file() or SOURCE_ROOT is None or q.is_absolute():
        return p
    cand = Path(SOURCE_ROOT) / p
    return str(cand) if cand.is_file() else p


def _norm_path(p: Any) -> str:
    try:
        return str(Path(str(p)).resolve()).lower().replace("\\", "/")
    except Exception:  # noqa: BLE001
        return str(p).lower()


def _compact_profile(res: dict) -> dict:
    """audio_profile: keep the summary; frames beyond 60 are averaged (power) onto a 1 s grid for the model."""
    fr = res.get("frames") or {}
    t = fr.get("t_s") or []
    if len(t) <= 60:
        return res
    import numpy as np
    k = 10
    nb = len(t) // k
    if nb == 0:
        return res
    bd = np.asarray(fr["band_db"], dtype=float)[: nb * k]          # (nb*k, n_bands)
    rd = np.asarray(fr["rms_db"], dtype=float)[: nb * k]
    pw = lambda d: 10 * np.log10(np.mean(10 ** (d / 10), axis=1))  # noqa: E731  power mean over the 10 frames
    out = dict(res)
    out["frames"] = {"t_s": [float(t[i * k]) for i in range(nb)],
                     "rms_db": pw(rd.reshape(nb, k)).tolist(),
                     "band_db": pw(bd.reshape(nb, k, -1)).tolist(),
                     "frame_s": 1.0,
                     "note": "agent view: 0.1 s frames power-averaged to 1 s; summary is exact"}
    return out


def _compact_changes(res: dict) -> dict:
    """detect_change_points: with > 15 points, drop the per-band before/after levels (delta_db stays)."""
    cps = res.get("change_points") or []
    if len(cps) <= 15:
        return res
    out = dict(res)
    out["change_points"] = [{k: v for k, v in c.items() if k not in ("before_db", "after_db")} for c in cps]
    out["note"] = "agent view: before_db/after_db omitted (more than 15 points); delta_db is exact"
    return out


_COMPACT = {"audio_profile": _compact_profile, "detect_change_points": _compact_changes}


def logged(fn):
    """Wrap an acoustic tool: resolve relative paths, round floats to 3 decimals, log the call, collect errors.

    The wrapper keeps the tool's name, signature and docstring, so the FunctionTool declaration is unchanged."""
    sig = inspect.signature(fn)
    name = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ba = sig.bind_partial(*args, **kwargs)
        for k in ("wav_path",):
            if k in ba.arguments:
                ba.arguments[k] = _resolve(ba.arguments[k])
        for k in ("paths_a", "paths_b"):
            if k in ba.arguments and isinstance(ba.arguments[k], (list, tuple)):
                ba.arguments[k] = [_resolve(p) for p in ba.arguments[k]]
        try:
            res = fn(*ba.args, **ba.kwargs)
        except Exception as e:  # noqa: BLE001  (the acoustic wrappers already catch; this is a second net)
            res = {"status": "error", "tool": name, "error": f"{type(e).__name__}: {e}"}
        if isinstance(res, dict) and res.get("status") != "error":
            res = _round(_COMPACT.get(name, lambda r: r)(res))
        args_d = {k: v for k, v in ba.arguments.items()}
        if len(_TOOL_LOG) >= _LOG_MAX:
            del _TOOL_LOG[0]
        _TOOL_LOG.append({"tool": name, "args": args_d, "result": res})
        if isinstance(res, dict) and res.get("status") == "error":
            path = args_d.get("wav_path") or ",".join(map(str, (args_d.get("paths_a") or [])[:1] + (args_d.get("paths_b") or [])[:1]))
            _TOOL_ERRORS.append({"path": str(path), "tool": name, "error": str(res.get("error"))[:500]})
        return res

    return wrapper


# ------------------------------------------------------------------------------- evidence verification
# Keys that identify the call or are prose; they are never compared as numbers.
_SKIP_KEYS = {"tool", "wav_path", "path", "paths_a", "paths_b", "method", "origin", "file", "folder", "note", "params"}
_ARG_ALIAS = {"claimed_N": "claimed_rpm", "Z": "flutes"}   # evidence key -> tool argument of the same quantity
_STR_KEYS = {"status", "verdict"}                         # string fields compared with the tool output


def _decimals(x: float) -> int:
    """Decimals written in a cited number (10400 -> 0, 12.13 -> 2, 12.0 -> 0); trailing zeros do not count."""
    try:
        exp = Decimal(repr(float(x))).normalize().as_tuple().exponent
    except (InvalidOperation, ValueError):
        return 6
    return max(0, min(6, -int(exp)))


def num_match(x: float, y: Any) -> bool:
    """A cited number x matches a tool value y iff |x - y| <= max(0.5 % of y, 0.01) AND x is y rounded to x's own
    decimals (a faithful rounding, never an approximation: 10431 for 10431.131 passes, 10400 and 12 for 12.13 do not)."""
    if isinstance(y, bool) or not isinstance(y, (int, float)) or not math.isfinite(float(y)):
        return False
    x, y = float(x), float(y)
    if abs(x - y) > max(MATCH_REL_TOL * abs(y), MATCH_ABS_TOL) + 1e-9:
        return False
    return abs(x - y) <= 0.5 * 10 ** (-_decimals(x)) + 1e-9


def _key_values(obj: Any, key: str, out: List[Any]) -> List[Any]:
    """Every value stored under `key` anywhere inside obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _key_values(v, key, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _key_values(v, key, out)
    return out


def _record_lists(obj: Any, out: List[List[dict]]) -> List[List[dict]]:
    """Every list of dicts inside obj (change_points, candidates, per_band ...)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _record_lists(v, out)
    elif isinstance(obj, list):
        if obj and all(isinstance(v, dict) for v in obj):
            out.append(obj)
        for v in obj:
            _record_lists(v, out)
    return out


def _leaves(e: Dict[str, Any]):
    """(key, value) pairs of an evidence item; nested dicts (claimed_check) are flattened to their own keys."""
    for k, v in e.items():
        if k in _SKIP_KEYS:
            continue
        if isinstance(v, dict):
            yield from _leaves(v)
        else:
            yield k, v


def _arg_value(rec: Dict[str, Any], key: str):
    a = rec["args"]
    k = _ARG_ALIAS.get(key, key)
    if k in a and a[k] is not None:
        return True, a[k]
    return False, None


def _field_ok(rec: Dict[str, Any], key: str, val: Any, records: Optional[List[dict]]) -> Optional[str]:
    """None if (key, val) is in this logged call (restricted to `records` for record fields), else the reason."""
    res = rec["result"]
    if isinstance(val, bool) or isinstance(val, str):
        if not (isinstance(val, bool) or key in _STR_KEYS or key in ("name_a", "name_b")):
            return None
        if key in ("name_a", "name_b"):
            has, av = _arg_value(rec, key)
            return None if has and str(av) == val else f"{key} {val!r} is not the {key} of the cited call"
        pool = _key_values(records if records is not None else res, key, [])
        if not pool:
            return None                       # key not in the output: nothing to compare
        return None if val in pool else f"{key}={val!r} not in the tool output (has {pool[:3]})"
    nums = _numbers(val)
    if not nums:
        return None
    has_arg, av = _arg_value(rec, key)
    if key in _ARG_ALIAS or (has_arg and not _key_values(res, key, [])):
        pool = _numbers(av) if has_arg else _numbers(_key_values(res, key, []))
    else:
        occ = _key_values(records if records is not None else res, key, [])
        pool = _numbers(occ) if occ else _numbers(res) + _numbers(
            {k: v for k, v in rec["args"].items() if k not in ("paths_a", "paths_b")})
    for x in nums:
        if not any(num_match(x, y) for y in pool):
            return f"{key}={x:g} not in the cited call's output under that field"
    return None


def _match_call(rec: Dict[str, Any], e: Dict[str, Any]) -> List[str]:
    """Problems of evidence item e against ONE logged call (empty list = every field comes from this call).

    Scalar fields that live inside a list of records (a change point's t_s, dominant_band_index, max_abs_delta_db)
    must all come from the SAME record; list fields (delta_db, subsynchronous_of) from the records so selected."""
    res = rec["result"]
    probs: List[str] = []
    rec_lists = _record_lists(res, [])
    record_keys = {}
    for L in rec_lists:
        for r in L:
            for k in r:
                record_keys.setdefault(k, L)
    groups: Dict[int, List] = {}
    for k, v in _leaves(e):
        L = record_keys.get(k)
        if L is not None and k not in res and k not in rec["args"]:
            groups.setdefault(id(L), [L, []])[1].append((k, v))
        else:
            p = _field_ok(rec, k, v, None)
            if p:
                probs.append(p)
    for L, kvs in groups.values():
        scal = [(k, v) for k, v in kvs if not isinstance(v, (list, tuple, dict))]
        lists = [(k, v) for k, v in kvs if isinstance(v, (list, tuple))]
        chosen = [r for r in L if all(_field_ok(rec, k, v, [r]) is None for k, v in scal)] if scal else list(L)
        if not chosen:
            probs.append("no single record of the cited call has " + ", ".join(f"{k}={v}" for k, v in scal))
            continue
        for k, v in lists:
            p = _field_ok(rec, k, v, chosen)
            if p:
                probs.append(p)
    return probs


def _window_overlaps(rec: Dict[str, Any], start: Optional[float], end: Optional[float]) -> bool:
    if start is None and end is None:
        return True
    res = rec["result"] if isinstance(rec["result"], dict) else {}
    a = res.get("t0_s", rec["args"].get("t0_s"))
    b = res.get("t1_s", rec["args"].get("t1_s"))
    a = 0.0 if a is None else float(a)
    b = math.inf if b is None else float(b)
    lo = start if start is not None else end
    hi = end if end is not None else start
    return lo <= b + 1e-6 and hi >= a - 1e-6


def check_evidence(evidence: List[Dict[str, Any]], default_path: Optional[str],
                   window: tuple = (None, None)) -> Dict[str, Any]:
    """Every evidence item must reproduce ONE logged call of the cited tool on the entry's own file.

    The call must be on the same file as the entry (an evidence wav_path naming another file, or a misspelled one,
    matches nothing - there is no fallback to other calls), its window must overlap the entry's window, its
    arguments must equal the cited ones (t0_s/t1_s, claimed_N/claimed_rpm, flutes ...), and every cited number must
    equal the value under the same field of that call within max(0.5 %, 0.01) and as a faithful rounding."""
    n, n_ok, unmatched = 0, 0, []
    entry_file = _norm_path(_resolve(default_path)) if default_path else None
    for e in evidence:
        tool = e.get("tool")
        n_e = len(_numbers({k: v for k, v in e.items() if k not in _SKIP_KEYS}))
        n += n_e
        ev_path = e.get("wav_path") or e.get("path")
        reason = None
        if tool != "class_separability":
            ev_file = _norm_path(_resolve(ev_path)) if ev_path else entry_file
            if entry_file and ev_file != entry_file:
                reason = f"evidence wav_path {ev_path!r} is not the entry's file {default_path!r}"
        if reason is None:
            calls = [r for r in _TOOL_LOG if r["tool"] == tool and isinstance(r["result"], dict)]
            if tool != "class_separability":
                target = _norm_path(_resolve(ev_path)) if ev_path else entry_file
                calls = [r for r in calls if _norm_path(r["args"].get("wav_path")) == target]
            calls = [r for r in calls if _window_overlaps(r, *window)]
            if not calls:
                reason = "no logged call of this tool on this file and window"
            else:
                best = min((_match_call(r, e) for r in calls), key=len)
                if best:
                    reason = "; ".join(best[:3])
        if reason:
            unmatched.append({"tool": tool, "reason": reason[:300]})
        else:
            n_ok += n_e
    return {"n_numbers": n, "n_matched": n_ok, "n_items": len(evidence),
            "n_items_matched": len(evidence) - len(unmatched), "unmatched": unmatched[:10],
            "verified": not unmatched and n > 0}


def boundary_audit(label_boundaries: Optional[Dict[str, List[float]]] = None,
                   tol_s: float = BOUNDARY_TOL_S) -> List[Dict[str, Any]]:
    """Per logged detect_change_points window on a labelled file: label.csv boundaries inside the window (at least
    coarse_window_s, default 2 s, from its edges, where a point can be found at all), how many have a change point
    within tol_s (matched) and how many do not (missed), plus the points far from every boundary."""
    lb = LABEL_BOUNDARIES if label_boundaries is None else {_norm_path(k): v for k, v in label_boundaries.items()}
    out, seen = [], set()
    for rec in _TOOL_LOG:
        if rec["tool"] != "detect_change_points" or not isinstance(rec["result"], dict):
            continue
        res, f = rec["result"], _norm_path(rec["args"].get("wav_path"))
        if f not in lb or res.get("status") != "ok":
            continue
        a, b = float(res.get("t0_s") or 0.0), float(res.get("t1_s") or 0.0)
        if (f, a, b) in seen:
            continue
        seen.add((f, a, b))
        cw = rec["args"].get("coarse_window_s", 2.0)
        edge = max(float(rec["args"].get("window_s") or 0.5), float(cw) if cw is not None else 0.0)
        pts = [float(c["t_s"]) for c in res.get("change_points") or []]
        checked = [x for x in lb[f] if a + edge <= x <= b - edge]
        missed = [x for x in checked if not any(abs(x - p) <= tol_s + 1e-9 for p in pts)]
        off = [p for p in pts if not any(abs(x - p) <= tol_s + 1e-9 for x in lb[f])]
        out.append({"wav_path": rec["args"].get("wav_path"), "t0_s": a, "t1_s": b, "tolerance_s": tol_s,
                    "edge_s": edge, "n_points": len(pts), "n_checked": len(checked),
                    "n_matched": len(checked) - len(missed), "n_missed": len(missed), "missed_s": missed,
                    "n_points_off_boundary": len(off), "points_off_boundary_s": off})
    return out


# ------------------------------------------------------------------------------- save tool
_COUNTS_PREFIX = re.compile(r"^\[computed by save_proposals:[^\]]*\]\s*")
_STATED_CP = re.compile(r"(\d+)\s+(?:[A-Za-z-]+\s+){0,3}change[ _-]?points?", re.I)


def _computed_counts(pf: "ProposalFile") -> Dict[str, Any]:
    c: Dict[str, Any] = {"proposals": len(pf.proposals),
                         "by_kind": {k: sum(1 for x in pf.proposals if x.kind == k) for k in KINDS},
                         "consistency_items": len(pf.consistency),
                         "consistency_by_result": {}, "errors_recorded": len(pf.errors)}
    for it in pf.consistency:
        c["consistency_by_result"][it.result] = c["consistency_by_result"].get(it.result, 0) + 1
    if pf.boundary_audit:
        c["boundaries"] = {k: sum(w[k] for w in pf.boundary_audit)
                           for k in ("n_checked", "n_matched", "n_missed", "n_points_off_boundary")}
        c["boundaries"]["windows"] = len(pf.boundary_audit)
    return c


def _counts_line(c: Dict[str, Any]) -> str:
    bk = c["by_kind"]
    parts = [f"{c['proposals']} proposals (change_point {bk['change_point']}, rpm_mismatch {bk['rpm_mismatch']}, "
             f"class_separability {bk['class_separability']}, question {bk['question']})"]
    if c["consistency_items"]:
        parts.append(f"{c['consistency_items']} consistency items "
                     + str(dict(sorted(c["consistency_by_result"].items()))).replace("'", ""))
    if "boundaries" in c:
        b = c["boundaries"]
        parts.append(f"label.csv boundaries in {b['windows']} change-point windows: checked {b['n_checked']}, "
                     f"matched {b['n_matched']}, missed {b['n_missed']} (within {BOUNDARY_TOL_S} s); "
                     f"{b['n_points_off_boundary']} points off every boundary")
    parts.append(f"{c['errors_recorded']} tool errors")
    return "[computed by save_proposals: " + "; ".join(parts) + "]"


def _boundary_warnings(pf: "ProposalFile") -> List[str]:
    """The agent's boundary_windows / consistency list against the deterministic audit."""
    w: List[str] = []
    for au in pf.boundary_audit:
        f = _norm_path(au["wav_path"])
        mine = [bw for bw in pf.boundary_windows
                if _norm_path(_resolve(bw.source_relative_path)) == f and abs(bw.t0_s - au["t0_s"]) <= 0.5
                and abs(bw.t1_s - au["t1_s"]) <= 0.5]
        tag = f"window {au['t0_s']:g}-{au['t1_s']:g} s"
        if not mine:
            w.append(f"{tag}: no boundary_windows entry (audit: checked {au['n_checked']}, matched "
                     f"{au['n_matched']}, missed {au['n_missed']})")
            continue
        bw = mine[0]
        if (bw.n_checked, bw.n_matched, bw.n_missed) != (au["n_checked"], au["n_matched"], au["n_missed"]):
            w.append(f"{tag}: boundary_windows says checked {bw.n_checked} / matched {bw.n_matched} / missed "
                     f"{bw.n_missed}, the audit of the tool output gives {au['n_checked']} / {au['n_matched']} / "
                     f"{au['n_missed']} (missed at {au['missed_s'][:20]})")
        recorded = 0
        for it in pf.consistency:
            if it.check != "change_point_vs_label_boundary":
                continue
            ts = [v for k, v in it.label_ref.items() if isinstance(v, (int, float)) and not isinstance(v, bool)
                  and k in ("boundary_s", "t_s", "start_s", "end_s", "time_s")]
            if any(au["t0_s"] + au["edge_s"] <= t <= au["t1_s"] - au["edge_s"] for t in ts):
                recorded += 1
        if recorded < au["n_checked"]:
            w.append(f"{tag}: {recorded} change_point_vs_label_boundary items for {au['n_checked']} checked "
                     f"boundaries - record every checked boundary, hit or miss")
    total_missed = sum(a["n_missed"] for a in pf.boundary_audit)
    if total_missed and pf.summary and re.search(r"\ball\b[^.]*\b(consistent|align)", pf.summary, re.I):
        w.append(f"summary claims all boundaries consistent/aligned, but {total_missed} checked boundaries were missed")
    return w


def save_proposals(proposals_json: str) -> dict:
    """Validate the evidence-proposal file and save it for human review (never applies anything).

    The file is a JSON object: source_id, source_dir, mode ('label_candidates' or 'consistency_only' for sources
    that already have label.csv), conditions (a JSON object, not a string), check_plan (list of {folder,
    folder_type, conditions (object), tools, files, sampling, reason}), proposals (list of {folder, session_id,
    source_relative_path, kind, start_s, end_s, candidate_label, confidence, evidence, rationale, question, figure}),
    consistency (list of {source_relative_path, check, label_ref, result, evidence, note}), boundary_windows
    (consistency_only: one per detect_change_points window, {source_relative_path, t0_s, t1_s, tolerance_s,
    n_checked, n_matched, n_missed, n_points_off_boundary, result, note}; result 'consistent' only when n_missed
    is 0), usage_limits_applied, summary. kind is one of change_point, rpm_mismatch, class_separability, question.
    evidence is a list of objects {"tool": <acoustic tool name>, "wav_path": <the entry's file>, ... fields copied
    from ONE call of that tool, under the tool's own field names ...}. candidate_label (0/1/2/-1) only on
    change_point entries. Forbidden anywhere: keys or values that write label.csv, change N or call a write tool
    (set_N, new_N, N_override, write_label_csv, label_csv, apply, overwrite, N, msm_label, convert_source,
    load_tables, save_mapping).

    Args:
        proposals_json: The full proposal file as a JSON string.

    Returns:
        dict with status 'saved_pending_approval' and the path, or 'invalid' with errors to fix. Also reports
        evidence that does not reproduce a logged tool call (evidence_warnings) and boundary counts that differ
        from the audit of the tool output (boundary_warnings); fix them and save again.
    """
    try:
        raw = json.loads(proposals_json)
    except Exception as e:  # noqa: BLE001
        return {"status": "invalid", "errors": f"not JSON: {e}"[:2000]}
    bad = find_forbidden_keys(raw)
    if bad:
        return {"status": "invalid", "errors": bad[:20], "rule": FEA013_MESSAGE}
    if isinstance(raw, dict):
        logged_errors = tool_errors()
        have = {(x.get("path"), x.get("tool")) for x in raw.get("errors") or [] if isinstance(x, dict)}
        raw.setdefault("errors", [])
        for x in logged_errors:                      # FEA-012: failed files are always recorded
            if (x["path"], x["tool"]) not in have:
                raw["errors"].append(x)
        raw.pop("boundary_audit", None)              # computed below, never taken from the model
        raw.pop("counts", None)
    try:
        pf = ProposalFile.model_validate(raw)
    except Exception as e:  # noqa: BLE001
        return {"status": "invalid", "errors": str(e)[:3000]}
    unverified = []
    for i, p in enumerate(pf.proposals):
        p.id = p.id or f"p{i + 1:03d}"
        p.evidence_check = check_evidence(p.evidence, p.source_relative_path, (p.start_s, p.end_s))
        if not p.evidence_check["verified"]:
            unverified.append({"id": p.id, "unmatched": p.evidence_check["unmatched"][:5]})
    for i, c in enumerate(pf.consistency):
        lr = c.label_ref or {}
        win = tuple(next((lr[k] for k in ks if isinstance(lr.get(k), (int, float)) and not isinstance(lr.get(k), bool)),
                         None) for ks in (("start_s", "row_start_s"), ("end_s", "row_end_s")))
        c.evidence_check = check_evidence(c.evidence, c.source_relative_path, win)
        if not c.evidence_check["verified"]:
            unverified.append({"consistency": i, "unmatched": c.evidence_check["unmatched"][:5]})
    if pf.mode == "consistency_only" and LABEL_BOUNDARIES:
        pf.boundary_audit = boundary_audit()
    bwarn = _boundary_warnings(pf) if pf.boundary_audit else []
    pf.counts = _computed_counts(pf)
    stated = [int(m.group(1)) for m in _STATED_CP.finditer(_COUNTS_PREFIX.sub("", pf.summary or ""))]
    swarn = [f"summary states {x} change points; the file holds {pf.counts['by_kind']['change_point']} change_point "
             f"entries" for x in stated if x != pf.counts["by_kind"]["change_point"]]
    pf.summary = _counts_line(pf.counts) + " " + _COUNTS_PREFIX.sub("", pf.summary or "").strip()
    pf.created_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    pf.model = pf.model or MODEL_NAME or os.environ.get("NORMALIZER_MODEL")
    p = work_dir() / "proposals" / f"{pf.source_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(pf.model_dump_json(indent=1), encoding="utf-8")
    out = {"status": "saved_pending_approval", "path": str(p), "n_proposals": len(pf.proposals),
           "n_consistency": len(pf.consistency), "n_errors_recorded": len(pf.errors),
           "kinds": pf.counts["by_kind"]}
    if "boundaries" in pf.counts:
        out["boundaries"] = pf.counts["boundaries"]
    if unverified:
        out["evidence_warnings"] = unverified[:15]
        out["hint"] = ("Some evidence does not reproduce a logged call of the cited tool on the entry's file. Copy "
                       "the fields exactly from ONE tool result, under the tool's own field names (do not compute "
                       "differences, averages or round coarsely) and save again.")
    if bwarn or swarn:
        out["boundary_warnings"] = (bwarn + swarn)[:15]
    return out
