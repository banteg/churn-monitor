from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DiffNode(BaseModel):
    id: str
    parent: str | None
    label: str
    path: str
    kind: Literal["root", "dir", "file"]
    value: int = Field(ge=0)
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    net_lines: int
    is_binary: bool = False
    previous_path: str | None = None


class CommitEntry(BaseModel):
    sha: str
    subject: str
    committed_at: datetime


class SnapshotSummary(BaseModel):
    commit_count: int = Field(ge=0)
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    net_lines: int
    changed_files: int = Field(ge=0)
    binary_files: int = Field(ge=0)


class DiffSnapshot(BaseModel):
    target_id: str
    repo_root: str
    worktree_path: str | None = None
    head_ref: str
    head_commit_at: datetime | None = None
    base_ref: str
    merge_base: str
    snapshot_key: str
    last_edit_at: datetime | None = None
    generated_at: datetime
    summary: SnapshotSummary
    commits: list[CommitEntry]
    nodes: list[DiffNode]


class MonitorTargetSummary(BaseModel):
    id: str
    head_ref: str
    worktree_path: str | None = None
    last_activity_at: datetime | None = None
    summary: SnapshotSummary
    is_current: bool = False


class MonitorOverview(BaseModel):
    selected_target_id: str
    targets: list[MonitorTargetSummary]
    snapshot: DiffSnapshot
