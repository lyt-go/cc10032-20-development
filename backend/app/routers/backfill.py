"""回填基准与快照覆盖专用路由。

必须在通用 resource 路由之前注册，否则 /api/backfill-jobs/... 会被
/api/{module} 动态路径抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import (
    BaselinePlanPayload,
    BaselineSearchPayload,
    CoverageRegisterPayload,
    SnapshotRevokePayload,
)
from app.services import service_for
from app.services.backfill_job import BackfillJobService
from app.services.snapshot_mgr import SnapshotMgrService

router = APIRouter(prefix="/api/backfill-jobs", tags=["回填基准"])


def backfill_service() -> BackfillJobService:
    service = service_for("backfill_job")
    assert isinstance(service, BackfillJobService)
    return service


def snapshot_service() -> SnapshotMgrService:
    service = service_for("snapshot_mgr")
    assert isinstance(service, SnapshotMgrService)
    return service


def ensure_ok(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        # 失败时保留完整载荷（如 window_failures、uncovered_windows），
        # detail 为对象时前端可以同时拿到原因与结构化明细。
        detail = {k: v for k, v in result.items() if k != "ok"}
        detail.setdefault("reason", "操作失败")
        raise HTTPException(status_code=400, detail=detail)
    return result


# ---------------- 快照覆盖注册表 ----------------
@router.get("/snapshot-registry")
def list_registry(table: str | None = None) -> dict[str, Any]:
    return snapshot_service().list_coverage(table)


@router.post("/snapshot-registry")
def register_coverage(payload: CoverageRegisterPayload) -> dict[str, Any]:
    return ensure_ok(snapshot_service().register_coverage(payload.model_dump()))


@router.post("/snapshot-registry/{snapshot_id}/revoke")
def revoke_snapshot(snapshot_id: str, payload: SnapshotRevokePayload) -> dict[str, Any]:
    return ensure_ok(snapshot_service().revoke_coverage(snapshot_id, payload.reason))


# ---------------- 回填基准检索 ----------------
@router.post("/{job_id}/baseline-search")
def baseline_search(job_id: int, payload: BaselineSearchPayload) -> dict[str, Any]:
    return ensure_ok(backfill_service().search_baselines(
        job_id,
        table=payload.table,
        start=payload.start,
        end=payload.end,
        windows=payload.windows or None,
        format_filter=payload.format_filter,
        readable_only=payload.readable_only,
    ))


@router.get("/baseline-queries/{query_id}")
def baseline_query(query_id: str) -> dict[str, Any]:
    return ensure_ok(backfill_service().get_baseline_query(query_id))


# ---------------- 回填清单与执行 ----------------
@router.get("/{job_id}/baseline-plan")
def get_plan(job_id: int) -> dict[str, Any]:
    service = backfill_service()
    plan = service.review_baseline(job_id)
    return ensure_ok(plan)


@router.post("/{job_id}/baseline-plan")
def sync_plan(job_id: int, payload: BaselinePlanPayload) -> dict[str, Any]:
    return ensure_ok(backfill_service().sync_baseline_plan(
        job_id,
        selection=payload.selection,
        source_query_id=payload.source_query_id,
        decision_mode=payload.decision_mode,
        decision_note=payload.decision_note,
    ))


@router.post("/{job_id}/execute")
def execute_backfill(job_id: int) -> dict[str, Any]:
    return ensure_ok(backfill_service().execute_backfill(job_id))


@router.get("/{job_id}/review")
def review_backfill(job_id: int) -> dict[str, Any]:
    return ensure_ok(backfill_service().review_baseline(job_id))
