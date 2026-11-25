"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class BaselineSearchPayload(BaseModel):
    """回填基准检索：目标表 + 缺失时段/数据窗口 + 格式与可读性筛选。"""
    table: str
    start: str | None = None
    end: str | None = None
    windows: list[dict[str, Any]] = Field(default_factory=list)
    format_filter: str | None = None
    readable_only: bool = True


class BaselinePlanPayload(BaseModel):
    """回填清单同步：选定快照组合 + 选择依据。"""
    selection: list[str] = Field(default_factory=list)
    source_query_id: str | None = None
    decision_mode: str | None = None
    decision_note: str = ""


class CoverageRegisterPayload(BaseModel):
    """快照覆盖登记。"""
    snapshot_id: str
    table: str
    version: str = ""
    aliases: list[str] = Field(default_factory=list)
    format: str = "parquet"
    readable: bool = True
    coverage: list[dict[str, Any]] = Field(default_factory=list)
    boundary_keys: dict[str, Any] = Field(default_factory=dict)
    boundary_corrected: bool = False


class SnapshotRevokePayload(BaseModel):
    reason: str = "快照失效"
