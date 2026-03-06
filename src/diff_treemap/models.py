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
    short_sha: str
    subject: str


class SnapshotSummary(BaseModel):
    commit_count: int = Field(ge=0)
    added_lines: int = Field(ge=0)
    deleted_lines: int = Field(ge=0)
    net_lines: int
    changed_files: int = Field(ge=0)
    binary_files: int = Field(ge=0)


class DiffSnapshot(BaseModel):
    repo_root: str
    head_ref: str
    base_ref: str
    merge_base: str
    snapshot_key: str
    last_edit_at: datetime | None = None
    generated_at: datetime
    summary: SnapshotSummary
    commits: list[CommitEntry]
    nodes: list[DiffNode]
