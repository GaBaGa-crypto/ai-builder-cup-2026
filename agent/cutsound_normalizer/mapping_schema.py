"""Mapping proposal schema.

Gemini proposes a Mapping (structured output); a human approves it; deterministic tools execute it.
Nothing in a Mapping changes data by itself.
"""
from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field


class MachineInfo(BaseModel):
    machine_id: str = Field(description="Short id used in output paths, e.g. 'ctwa_mill'")
    display_name: Optional[str] = Field(default=None, description="Display name for the viewer, e.g. 'CTWA mill'; "
                                        "defaults to model, then machine_id")
    model: Optional[str] = Field(default=None, description="Machine model if stated by the source")
    process: Literal["milling", "turning", "drilling", "grinding", "other", "unknown"] = "unknown"
    field: Literal["lab", "industry", "unknown"] = "unknown"
    coolant: Literal["wet", "dry", "unknown"] = "unknown"


class SessionRule(BaseModel):
    grouping: Literal["per_file", "msm_session"] = Field(
        description="per_file: every audio file becomes one session; msm_session: source already uses MSM session folders")
    audio_glob: str = Field(description="Glob relative to the source root that selects the airborne audio files")


class AudioRule(BaseModel):
    channel: Literal["mean", "first", "second"] = Field(
        default="mean", description="How to reduce multi-channel audio to mono")
    mic_type: Literal["airborne", "contact", "acoustic_emission", "unknown"] = "unknown"
    gain: Literal["none", "peak"] = Field(
        default="none", description="'none' keeps the recorded level (recommended); 'peak' scales the file peak to -1 dBFS")


class LabelRule(BaseModel):
    native: str = Field(description="Native label token exactly as captured by label_pattern")
    msm_label: Literal[0, 1, 2, -1] = Field(description="MSM label; -1 if it cannot be mapped honestly")
    cut_state: Literal["cutting", "non_cutting", "unknown"]
    anomaly_type: Optional[str] = None
    confidence: Literal["exact", "expert_rule", "threshold", "none"]
    rationale: str = Field(description="Why this mapping; cite the source text or folder name")


class FieldRule(BaseModel):
    msm_field: Literal["N", "ap", "ae", "F", "Z", "D", "Tool", "Workpiece", "direction"]
    source: Literal["constant", "path_regex", "unknown"]
    value: Optional[Union[str, float]] = Field(default=None, description="For source='constant'")
    pattern: Optional[str] = Field(default=None, description="For source='path_regex': regex on the relative path with a named group 'v'")
    unit_in: Optional[str] = Field(default=None, description="Unit as stated by the source")
    factor: float = Field(default=1.0, description="Multiply the source value by this to reach the MSM unit")
    evidence: str = Field(description="Where this came from (quote or file name)")
    applies_to: Optional[str] = Field(default=None, description="Optional regex on the relative path; the rule is used only "
                                      "for matching files. Later rules override earlier ones (use for per-file corrections)")


class IntervalLabel(BaseModel):
    """A labelled time span inside one source file (e.g. from a spectrogram review). Overrides the file-level rule."""
    path: str = Field(description="Source path relative to the source root, '/' separators")
    start_s: float = Field(ge=0)
    end_s: Optional[float] = Field(default=None, description="None = to the end of the file")
    msm_label: Literal[0, 1, 2, -1]
    cut_state: Literal["cutting", "non_cutting", "unknown"] = "cutting"
    anomaly_type: Optional[str] = None
    confidence: Literal["exact", "expert_rule", "threshold", "none"] = "expert_rule"
    evidence: str = Field(description="How the time was found (who, which plot/feature)")


class Mapping(BaseModel):
    mapping_version: str = "0.1"
    source_id: str
    source_name: str
    url: Optional[str] = None
    license: Optional[str] = None
    citation: Optional[str] = None
    machine: MachineInfo
    session_rule: SessionRule
    audio: AudioRule
    label_source: Literal["path_regex", "msm_label_csv"]
    label_pattern: Optional[str] = Field(default=None, description="For path_regex: regex with named group 'v' capturing the native label token")
    label_rules: List[LabelRule] = Field(default_factory=list)
    event_granularity: Literal["file", "interval"] = "file"
    fields: List[FieldRule] = Field(default_factory=list)
    interval_labels: List[IntervalLabel] = Field(default_factory=list,
                                                 description="Optional time-span labels inside files; override file-level rules")
    unresolved: List[str] = Field(default_factory=list, description="Questions a human must answer before conversion")
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
