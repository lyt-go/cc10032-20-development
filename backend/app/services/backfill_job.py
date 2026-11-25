"""backfill_job 领域服务：历史数据回填任务。

业务逻辑：
1. 回填任务：基于快照基准向目标表回填历史数据
2. 回填链路：snapshot_mgr → backfill_job（使用快照作为数据基准）
3. 基准检索：不再只检查「有没有快照」，而是按缺失时段的数据窗口，
   依据快照的实际覆盖范围检索基准，区分完整覆盖、部分覆盖与被排除版本
4. 回填清单：选定的快照组合同步到清单，跨版本交接位置只算一次，
   回填执行只读取清单里登记的实际版本
5. 失效保留：源快照撤销后清单原选单不删除，窗口失败原因可查，旧查询结论可追溯

跨模块数据流：
  snapshot_mgr（覆盖注册表）→ backfill_job（基准检索 → 清单 → 执行记录）
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from app.services import snapshot_search
from app.services.base import BaseModuleService
from app.store import store


class BackfillJobService(BaseModuleService):
    MODULE = "backfill_job"
    ENTITY = "回填任务"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["任务编号", "目标表", "回填起始时间", "回填结束时间", "源表列表", "依赖检查", "并行度", "回填水位"]
    REQUIRED_FIELDS = ["任务编号", "目标表", "回填起始时间"]
    STATUS_ORDER = ["待执行", "运行中", "已完成", "已失败"]
    ACTION_RULES = {"发起回填": "运行中", "标记完成": "已完成", "标记失败": "已失败"}
    NEGATIVE_ACTIONS = []

    PIPELINE = "backfill_pipeline"
    UPSTREAM_MODULES = ["snapshot_mgr"]
    PRODUCES_ARTIFACT = "回填执行记录"

    # ================= 基准快照检索 =================
    def search_baselines(self, job_id: int | None, *, table: str,
                         start: str | None = None, end: str | None = None,
                         windows: list[dict[str, Any]] | None = None,
                         format_filter: str | None = None,
                         readable_only: bool = True) -> dict[str, Any]:
        """按目标表、缺失时段与可读格式筛选，按覆盖数据窗口排序检索快照。"""
        try:
            win_list = snapshot_search.normalize_windows(windows, start, end)
        except ValueError as exc:
            return {"ok": False, "reason": str(exc)}

        if format_filter and format_filter not in snapshot_search.READABLE_FORMATS:
            return {"ok": False, "reason": f"不支持的压缩格式筛选：{format_filter}"}

        result = snapshot_search.search_snapshots(
            store.snapshot_coverage_registry(),
            table=table, windows=win_list,
            format_filter=format_filter, readable_only=readable_only,
        )
        query_id = uuid.uuid4().hex[:12]
        record = store.append_baseline_query({
            "query_id": query_id,
            "job_id": job_id,
            "table": table,
            "windows": result["windows"],
            "format_filter": format_filter,
            "readable_only": readable_only,
            "ranked_snapshots": result["ranked_snapshots"],
            "partial_snapshots": result["partial_snapshots"],
            "excluded_snapshots": result["excluded_snapshots"],
            "suggested_selection": result["suggested_selection"],
            "suggested_handoff_points": result["suggested_handoff_points"],
            "uncovered_windows": result["uncovered_windows"],
            "full_coverage_available": result["full_coverage_available"],
        })
        result["ok"] = True
        result["query_id"] = query_id
        result["query_history_note"] = "本次查询结论已归档，可通过 query_id 复核"
        result["archived_query"] = record
        return result

    def get_baseline_query(self, query_id: str) -> dict[str, Any]:
        """读取历史查询结论：旧结论在新版本登记后依然可查。"""
        record = store.baseline_query(query_id)
        if record is None:
            return {"ok": False, "reason": f"查询记录 {query_id} 不存在"}
        return {"ok": True, "query": record}

    # ================= 回填基准清单 =================
    def sync_baseline_plan(self, job_id: int, *,
                           selection: list[str], source_query_id: str | None = None,
                           decision_mode: str | None = None,
                           decision_note: str = "") -> dict[str, Any]:
        """把选定的快照组合同步到回填清单。

        - 交接位置按版本切换边界去重，只算一次
        - 回填未开始（待执行）才允许重新选择；开始后清单冻结
        """
        entry = store.find(self.MODULE, job_id)
        if entry is None:
            return {"ok": False, "reason": f"任务 {job_id} 不存在"}
        if entry.get("status") != "待执行":
            return {"ok": False,
                    "reason": f"任务状态 {entry.get('status')}，回填已开始，基准清单不可再变更"}
        if not selection:
            return {"ok": False, "reason": "选定快照为空，无法同步回填清单"}

        start, end = entry.get("回填起始时间"), entry.get("回填结束时间")
        try:
            windows = snapshot_search.normalize_windows(None, start, end)
        except ValueError as exc:
            return {"ok": False,
                    "reason": f"任务缺失时段不完整，无法核对清单覆盖：{exc}"}
        chosen: list[dict[str, Any]] = []
        window_failures: list[dict[str, Any]] = []
        for snapshot_id in selection:
            record = store.coverage_record(str(snapshot_id))
            if record is None:
                window_failures.append({
                    "snapshot_id": str(snapshot_id), "window": "*",
                    "reason": "快照未在覆盖注册表登记，无法定位实际版本",
                })
                continue
            if record.get("revoked"):
                window_failures.append({
                    "snapshot_id": str(snapshot_id),
                    "window": record.get("coverage", []),
                    "reason": f"快照已撤销：{record.get('revoke_reason', '未注明原因')}",
                })
                continue
            if not record.get("readable", True):
                window_failures.append({
                    "snapshot_id": str(snapshot_id),
                    "window": record.get("coverage", []),
                    "reason": "快照不可读，无法作为回填基准",
                })
                continue
            chosen.append(record)
        if window_failures:
            return {"ok": False, "reason": "选定快照存在失效项", "window_failures": window_failures}

        # 覆盖完整性校验：组合必须覆盖全部缺失窗口
        merged = snapshot_search.merge_ranges(
            [rng for record in chosen for rng in snapshot_search.snapshot_ranges(record)]
        )
        uncovered = []
        for win in windows:
            state, _ = snapshot_search.window_cover_state(win, merged)
            if state != "full":
                uncovered.append(win.readable())
        if uncovered:
            return {"ok": False,
                    "reason": "选定组合未完整覆盖缺失窗口",
                    "uncovered_windows": uncovered}

        handoff = snapshot_search.handoff_points([
            {"snapshot_id": r["snapshot_id"], "coverage": r.get("coverage", [])}
            for r in chosen
        ])
        mode = decision_mode or ("single" if len(chosen) == 1 else "combination")
        plan = {
            "job_id": job_id,
            "table": str(entry.get("目标表", "")),
            "windows": [{"start": w.start, "end": w.end, "label": w.label,
                         "readable": w.readable()} for w in windows],
            "selection": [{
                "snapshot_id": r["snapshot_id"],
                "version": r.get("version", ""),
                "format": r.get("format", ""),
                "coverage": r.get("coverage", []),
                "boundary_keys": r.get("boundary_keys", {}),
                "boundary_corrected": r.get("boundary_corrected", False),
                "registered_at": r.get("registered_at", 0),
            } for r in chosen],
            "handoff_points": handoff,
            "handoff_count": len(handoff),
            "source_query_id": source_query_id,
            "decision_mode": mode,
            "decision_note": decision_note,
            "synced_at": time.time(),
            "status": "active",
            "window_failures": [],
            "executed": False,
            "execution_id": None,
        }
        saved = store.save_backfill_plan(job_id, plan)
        self._write_plan_summary(entry, saved)
        return {"ok": True, "plan": saved}

    def review_baseline(self, job_id: int) -> dict[str, Any]:
        """复核回填任务的基准清单、历次查询结论与执行记录。"""
        entry = store.find(self.MODULE, job_id)
        if entry is None:
            return {"ok": False, "reason": f"任务 {job_id} 不存在"}
        plan = store.backfill_plan(job_id)
        live_failures = self._plan_window_failures(plan) if plan else []
        if plan and live_failures:
            plan["status"] = "invalid"
            plan["window_failures"] = live_failures
            store.save_backfill_plan(job_id, plan)
            self._write_plan_summary(entry, plan)
        return {
            "ok": True,
            "job_id": job_id,
            "entry_brief": {
                "任务编号": entry.get("任务编号"),
                "目标表": entry.get("目标表"),
                "status": entry.get("status"),
                "依赖检查": entry.get("依赖检查"),
            },
            "plan": plan,
            "query_history": store.baseline_queries_for_job(job_id),
            "executions": store.backfill_executions(job_id),
        }

    # ================= 执行回填 =================
    def execute_backfill(self, entry_id: int) -> dict[str, Any]:
        """执行回填：有清单时严格读取清单登记的实际版本；无清单时走兼容依赖检查。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "reason": f"任务 {entry_id} 不存在"}
        if entry.get("status") != "待执行":
            return {"ok": False, "reason": f"任务状态 {entry.get('status')} 非待执行"}

        target_table = str(entry.get("目标表", ""))
        parallelism = int(str(entry.get("并行度", "1")))
        if parallelism > 10:
            return {"ok": False, "reason": f"并行度 {parallelism} 超限（最大10）"}

        plan = store.backfill_plan(entry_id)
        if plan is not None:
            return self._execute_with_plan(entry, plan, parallelism)
        return self._execute_legacy(entry, target_table)

    def _execute_with_plan(self, entry: dict[str, Any], plan: dict[str, Any],
                           parallelism: int) -> dict[str, Any]:
        """按回填清单执行：实际版本以清单为准，源快照失效则保留选单并记录窗口失败。"""
        job_id = int(entry["id"])
        failures = self._plan_window_failures(plan)
        if failures:
            plan["status"] = "invalid"
            plan["window_failures"] = failures
            store.save_backfill_plan(job_id, plan)
            entry["依赖检查"] = "失败: 基准快照失效，详见回填清单窗口失败原因"
            entry["status"] = "已失败"
            entry["abnormal"] = True
            execution = store.append_backfill_execution({
                "job_id": job_id, "ok": False,
                "reason": "基准快照失效，回填中止",
                "window_failures": failures,
                "plan_selection": plan["selection"],
                "decision_mode": plan.get("decision_mode"),
                "decision_note": plan.get("decision_note", ""),
            })
            self._write_plan_summary(entry, plan)
            return {"ok": False, "reason": "基准快照失效，回填中止",
                    "window_failures": failures, "execution_id": execution.get("executed_at")}

        # 实际边界键取自清单快照登记值，作为执行核对依据
        boundary_actual = []
        for sel in plan["selection"]:
            boundary_actual.append({
                "snapshot_id": sel["snapshot_id"],
                "version": sel["version"],
                "boundary_corrected": sel.get("boundary_corrected", False),
                "boundary_keys": sel.get("boundary_keys", {}),
            })

        rows_to_insert = self._build_rows(entry, len(plan["selection"]) * 10)
        execution = store.append_backfill_execution({
            "job_id": job_id,
            "ok": True,
            "table": plan["table"],
            "actual_versions": [
                {"snapshot_id": s["snapshot_id"], "version": s["version"]}
                for s in plan["selection"]
            ],
            "boundary_key_actual": boundary_actual,
            "selection_rationale": plan.get("decision_note") or self._default_rationale(plan),
            "decision_mode": plan.get("decision_mode"),
            "handoff_points": plan.get("handoff_points", []),
            "handoff_count": plan.get("handoff_count", 0),
            "rows_inserted": len(rows_to_insert),
        })
        plan["executed"] = True
        plan["execution_id"] = execution["executed_at"]
        plan["status"] = "executed"
        store.save_backfill_plan(job_id, plan)

        entry["依赖检查"] = "通过"
        entry["回填水位"] = str(time.time())
        entry["status"] = "运行中"
        entry["pending"] = True
        self._write_plan_summary(entry, plan)

        batch_id = f"BACKFILL_{job_id}_{int(time.time())}"
        self.produce_artifact(rows_to_insert, batch_id)
        return {
            "ok": True,
            "entry_id": job_id,
            "target_table": plan["table"],
            "rows_inserted": len(rows_to_insert),
            "actual_versions": execution["actual_versions"],
            "boundary_key_actual": boundary_actual,
            "handoff_points": plan["handoff_points"],
            "decision_mode": plan["decision_mode"],
            "selection_rationale": execution["selection_rationale"],
        }

    def _execute_legacy(self, entry: dict[str, Any], target_table: str) -> dict[str, Any]:
        """兼容旧链路：只依赖 _snapshot_store 有无快照的检查方式。"""
        source_tables = str(entry.get("源表列表", "")).split(",")
        deps_ok, dep_issues = True, []
        for st in source_tables:
            st = st.strip()
            if not st:
                continue
            has_snapshot = any(snap.get("table") == st for snap in store._snapshot_store.values())
            if not has_snapshot:
                dep_issues.append(f"源表 {st} 无可用快照")
                deps_ok = False
        if not deps_ok:
            entry["依赖检查"] = "失败: " + "; ".join(dep_issues)
            entry["status"] = "已失败"
            entry["abnormal"] = True
            return {"ok": False, "reason": "; ".join(dep_issues)}

        rows_to_insert = self._build_rows(entry, 20)
        entry["依赖检查"] = "通过"
        entry["回填水位"] = str(time.time())
        entry["status"] = "运行中"
        entry["pending"] = True
        batch_id = f"BACKFILL_{entry['id']}_{int(time.time())}"
        self.produce_artifact(rows_to_insert, batch_id)
        return {
            "ok": True,
            "entry_id": entry["id"],
            "target_table": target_table,
            "rows_inserted": len(rows_to_insert),
            "source_tables": source_tables,
            "dependencies_passed": len(source_tables),
        }

    # ================= 内部辅助 =================
    @staticmethod
    def _plan_window_failures(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
        """核对清单快照当前是否仍有效，失效项转换为窗口失败原因，原选单保留。"""
        if not plan:
            return []
        failures = []
        for sel in plan.get("selection", []):
            record = store.coverage_record(str(sel["snapshot_id"]))
            if record is None:
                failures.append({
                    "snapshot_id": sel["snapshot_id"],
                    "window": sel.get("coverage", []),
                    "reason": "快照注册记录丢失，实际版本不可定位",
                })
            elif record.get("revoked"):
                failures.append({
                    "snapshot_id": sel["snapshot_id"],
                    "window": sel.get("coverage", []),
                    "reason": f"源快照已撤销：{record.get('revoke_reason', '未注明原因')}",
                })
            elif not record.get("readable", True):
                failures.append({
                    "snapshot_id": sel["snapshot_id"],
                    "window": sel.get("coverage", []),
                    "reason": "源快照已不可读",
                })
        return failures

    @staticmethod
    def _write_plan_summary(entry: dict[str, Any], plan: dict[str, Any]) -> None:
        versions = "、".join(f"{s['snapshot_id']}({s['version']})" for s in plan["selection"])
        entry["基准快照清单"] = versions
        entry["基准状态"] = plan["status"]
        entry["基准选择依据"] = plan.get("decision_note") or ""
        if plan["status"] == "invalid":
            entry["依赖检查"] = "失败: 基准快照失效，详见回填清单"
        elif not plan.get("executed"):
            entry["依赖检查"] = (
                f"清单就绪: {len(plan['selection'])}个版本, "
                f"交接点{plan['handoff_count']}个"
            )

    @staticmethod
    def _default_rationale(plan: dict[str, Any]) -> str:
        if plan.get("decision_mode") == "single":
            return "单版本完整覆盖全部缺失窗口，直接作为回填基准"
        return (f"{len(plan['selection'])}个快照按覆盖窗口组合覆盖全段，"
                f"跨版本交接位置 {plan.get('handoff_count', 0)} 个（已去重）")

    @staticmethod
    def _build_rows(entry: dict[str, Any], count: int) -> list[dict[str, Any]]:
        rows = []
        for i in range(count):
            rows.append({
                "table": entry.get("目标表"),
                "batch": entry.get("任务编号"),
                "row": i,
                "ts": time.time() - i * 3600,
                "backfilled": True,
            })
        return rows

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["snapshot_dependencies"] = len(store._snapshot_store)
        base["baseline_plans"] = len(store.all_backfill_plans())
        base["baseline_queries"] = len(store._baseline_query_history)
        base["backfill_executions"] = len(store.backfill_executions())
        return base
