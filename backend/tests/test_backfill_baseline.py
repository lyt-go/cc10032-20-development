"""回填基准检索端到端验收测试。

验收场景（对应需求描述）：
1. 两个区间交叠的有效快照 + 一个同名不可读旧版本，检索出需要组合覆盖的方案
2. 选择覆盖两个窗口的组合并同步到回填清单，跨版本交接位置只算一次
3. 回填未开始时登记一份覆盖全段、边界键已更正的新版本，可重新选择
4. 实际开始后核对边界键实际值与选择依据
5. 撤销一个历史版本后复核该次记录：原选单保留、窗口失败原因可查、旧查询结论可追溯
"""
from __future__ import annotations

import pytest

from app.main import app
from app.services import service_for
from app.services import snapshot_search
from app.store import store


@pytest.fixture()
def acceptance_table() -> str:
    """构造一份独立验收数据，不与 seed 注册表冲突。"""
    table = "acc_backfill_table"
    registry = [
        {
            "snapshot_id": "ACC-A", "table": table, "version": "v1",
            "aliases": ["acc-half1"], "format": "parquet", "readable": True,
            # 与 ACC-B 在 7 月上半月交叠
            "coverage": [{"start": "2026-01-01", "end": "2026-07-15"}],
            "boundary_keys": {"handoff_order_id": "ORD-700001",
                              "window_end": "2026-07-15"},
            "boundary_corrected": False, "registered_at": 1000,
        },
        {
            "snapshot_id": "ACC-B", "table": table, "version": "v2",
            "aliases": ["acc-half2"], "format": "orc", "readable": True,
            "coverage": [{"start": "2026-07-01", "end": "2026-12-31"}],
            "boundary_keys": {"handoff_order_id": "ORD-800002",
                              "window_start": "2026-07-01"},
            "boundary_corrected": False, "registered_at": 2000,
        },
        # 与 ACC-A 同名版本 v1 的不可读旧版本，必须被排除
        {
            "snapshot_id": "ACC-C", "table": table, "version": "v1",
            "aliases": [], "format": "parquet", "readable": False,
            "coverage": [{"start": "2026-01-01", "end": "2026-06-30"}],
            "boundary_keys": {}, "boundary_corrected": False,
            "registered_at": 500,
        },
    ]
    for item in registry:
        store.register_snapshot_coverage(item)

    job_id = max((int(r["id"]) for r in store.rows("backfill_job")), default=0) + 1
    store.rows("backfill_job").append({
        "id": job_id, "status": "待执行", "pending": True, "abnormal": False,
        "任务编号": f"BACK-ACC-{job_id}", "目标表": table,
        "回填起始时间": "2026-01-01", "回填结束时间": "2026-12-31",
        "源表列表": table, "依赖检查": "", "并行度": "2", "回填水位": "",
    })
    yield table
    # 清理验收数据，保持其它测试隔离
    for snap_id in ("ACC-A", "ACC-B", "ACC-C", "ACC-D"):
        store._snapshot_coverage.pop(snap_id, None)
    store._backfill_plans.pop(job_id, None)


@pytest.fixture()
def acceptance_job_id(acceptance_table: str) -> int:
    rows = [r for r in store.rows("backfill_job")
            if r.get("任务编号", "").startswith("BACK-ACC-")]
    return int(rows[-1]["id"])


class TestBaselineSearch:
    def test_search_ranks_by_covered_windows_and_excludes_unreadable(
        self, acceptance_table, acceptance_job_id
    ):
        bj = service_for("backfill_job")
        body = bj.search_baselines(
            acceptance_job_id, table=acceptance_table,
            windows=[
                {"start": "2026-01-01", "end": "2026-06-30", "label": "上半年窗口"},
                {"start": "2026-07-01", "end": "2026-12-31", "label": "下半年窗口"},
            ],
        )
        assert body["ok"] is True
        # 两个有效快照各完整覆盖一个窗口，进入排序候选
        ranked_ids = {item["snapshot_id"] for item in body["ranked_snapshots"]}
        assert ranked_ids == {"ACC-A", "ACC-B"}
        for item in body["ranked_snapshots"]:
            assert item["full_count"] == 1
        # 同名不可读旧版本被排除并给出原因
        excluded = {e["snapshot_id"]: e for e in body["excluded_snapshots"]}
        assert "ACC-C" in excluded
        assert excluded["ACC-C"]["reason_code"] == "unreadable"
        # 没有单版本全覆盖，推荐两个快照的组合
        assert body["full_coverage_available"] is False
        assert set(body["suggested_selection"]) == {"ACC-A", "ACC-B"}
        # 交接位置只算一次：版本 A 覆盖段的结束边界 2026-07-15
        assert body["suggested_handoff_points"] == ["2026-07-15"]
        assert body["suggested_handoff_count"] == 1
        # 可读格式筛选：orc 只留下 ACC-B，ACC-A 因格式不符被排除
        fmt_body = bj.search_baselines(
            acceptance_job_id, table=acceptance_table,
            start="2026-01-01", end="2026-12-31",
            format_filter="orc",
        )
        assert [i["snapshot_id"] for i in fmt_body["ranked_snapshots"]] == ["ACC-B"]
        assert any(
            e["snapshot_id"] == "ACC-A" and e["reason_code"] == "format_mismatch"
            for e in fmt_body["excluded_snapshots"]
        )

    def test_router_registers_backfill_endpoints(self):
        """专用路由在通用动态路由之前注册，路径可解析到回填服务。"""
        paths, router_order = [], []
        for route in app.routes:
            if type(route).__name__ != "_IncludedRouter":
                continue
            sub_paths = [sub.path for sub in route.original_router.routes]
            router_order.append(sub_paths[0])
            paths.extend(sub_paths)
        assert "/api/backfill-jobs/{job_id}/baseline-search" in paths
        assert "/api/backfill-jobs/snapshot-registry" in paths
        # backfill 专用路由先于通用 /api/{module} 动态路由注册
        assert router_order[0].startswith("/api/backfill-jobs")


    def test_partial_window_snapshots_listed_separately(
        self, acceptance_table, acceptance_job_id
    ):
        store.register_snapshot_coverage({
            "snapshot_id": "ACC-P", "table": acceptance_table, "version": "v9",
            "aliases": [], "format": "parquet", "readable": True,
            "coverage": [{"start": "2026-06-15", "end": "2026-07-15"}],
            "boundary_keys": {}, "boundary_corrected": False,
            "registered_at": 100,
        })
        try:
            bj = service_for("backfill_job")
            result = bj.search_baselines(
                acceptance_job_id, table=acceptance_table,
                start="2026-01-01", end="2026-12-31",
            )
            partial_ids = {p["snapshot_id"] for p in result["partial_snapshots"]}
            assert "ACC-P" in partial_ids
            assert "ACC-P" not in {r["snapshot_id"] for r in result["ranked_snapshots"]}
        finally:
            store._snapshot_coverage.pop("ACC-P", None)

    def test_alias_ambiguity_excludes_both_owners(self, acceptance_job_id):
        table = "acc_alias_table"
        for sid, version in (("AL-A", "v1"), ("AL-B", "v2")):
            store.register_snapshot_coverage({
                "snapshot_id": sid, "table": table, "version": version,
                "aliases": ["shared-alias"], "format": "parquet", "readable": True,
                "coverage": [{"start": "2026-01-01", "end": "2026-06-30"}],
                "boundary_keys": {}, "boundary_corrected": False,
                "registered_at": 100,
            })
        try:
            bj = service_for("backfill_job")
            result = bj.search_baselines(
                acceptance_job_id, table=table,
                start="2026-01-01", end="2026-06-30",
            )
            codes = {e["snapshot_id"]: e["reason_code"]
                     for e in result["excluded_snapshots"]}
            assert codes.get("AL-A") == "alias_ambiguous"
            assert codes.get("AL-B") == "alias_ambiguous"
            assert result["ranked_snapshots"] == []
        finally:
            for sid in ("AL-A", "AL-B"):
                store._snapshot_coverage.pop(sid, None)


class TestPlanSyncAndExecution:
    def test_full_acceptance_flow(self, acceptance_table, acceptance_job_id):
        bj = service_for("backfill_job")

        # 1. 检索并选择覆盖两个窗口的组合，同步到回填清单
        search = bj.search_baselines(
            acceptance_job_id, table=acceptance_table,
            start="2026-01-01", end="2026-12-31",
        )
        assert set(search["suggested_selection"]) == {"ACC-A", "ACC-B"}
        first_query_id = search["query_id"]

        sync = bj.sync_baseline_plan(
            acceptance_job_id,
            selection=["ACC-A", "ACC-B"],
            source_query_id=first_query_id,
            decision_mode="combination",
            decision_note="两份快照各覆盖半年，组合覆盖全段",
        )
        assert sync["ok"] is True
        plan = sync["plan"]
        assert [s["snapshot_id"] for s in plan["selection"]] == ["ACC-A", "ACC-B"]
        assert plan["handoff_points"] == ["2026-07-15"]
        assert plan["handoff_count"] == 1
        assert plan["executed"] is False
        entry = store.find("backfill_job", acceptance_job_id)
        assert "清单就绪" in entry["依赖检查"]
        assert "ACC-A(v1)" in entry["基准快照清单"]

        # 2. 回填未开始：登记一份覆盖全段、边界键已更正的新版本
        store.register_snapshot_coverage({
            "snapshot_id": "ACC-D", "table": acceptance_table, "version": "v3",
            "aliases": ["acc-full"], "format": "parquet", "readable": True,
            "coverage": [{"start": "2026-01-01", "end": "2026-12-31"}],
            "boundary_keys": {"handoff_order_id": "ORD-999999",
                              "window_start": "2026-01-01",
                              "window_end": "2026-12-31"},
            "boundary_corrected": True, "registered_at": 3000,
        })
        search2 = bj.search_baselines(
            acceptance_job_id, table=acceptance_table,
            start="2026-01-01", end="2026-12-31",
        )
        assert search2["full_coverage_available"] is True
        assert search2["ranked_snapshots"][0]["snapshot_id"] == "ACC-D"
        # 重新选择单版本方案
        sync2 = bj.sync_baseline_plan(
            acceptance_job_id,
            selection=["ACC-D"],
            source_query_id=search2["query_id"],
            decision_mode="single",
            decision_note="新版本 v3 单份覆盖全段且边界键已更正，改用单版本",
        )
        assert sync2["ok"] is True
        assert sync2["plan"]["selection"][0]["snapshot_id"] == "ACC-D"
        assert sync2["plan"]["handoff_count"] == 0

        # 3. 实际开始回填：核对边界键实际值与选择依据
        executed = bj.execute_backfill(acceptance_job_id)
        assert executed["ok"] is True
        assert executed["actual_versions"] == [
            {"snapshot_id": "ACC-D", "version": "v3"}
        ]
        actual_boundary = executed["boundary_key_actual"][0]
        assert actual_boundary["boundary_corrected"] is True
        assert actual_boundary["boundary_keys"]["handoff_order_id"] == "ORD-999999"
        assert "边界键已更正" in executed["selection_rationale"]
        plan_after = store.backfill_plan(acceptance_job_id)
        assert plan_after["status"] == "executed"
        assert plan_after["executed"] is True

        # 4. 撤销一个历史版本（ACC-A），复核该次记录
        revoke = service_for("snapshot_mgr").revoke_coverage(
            "ACC-A", "底层文件被生命周期清理")
        assert revoke["ok"] is True
        review = bj.review_baseline(acceptance_job_id)
        assert review["ok"] is True
        # 原选单仍在，且实际执行的版本未被改写
        assert review["plan"]["selection"][0]["snapshot_id"] == "ACC-D"
        # 执行记录完整保留
        assert review["executions"][0]["ok"] is True
        assert review["executions"][0]["actual_versions"][0]["snapshot_id"] == "ACC-D"
        # 旧查询结论依然可查
        old_query = bj.get_baseline_query(first_query_id)
        assert old_query["ok"] is True
        assert set(old_query["query"]["suggested_selection"]) == {"ACC-A", "ACC-B"}

    def test_invalidated_snapshot_before_execution_keeps_selection_and_reason(
        self, acceptance_table, acceptance_job_id
    ):
        """源快照失效（执行前）：清单原选单保留，窗口失败原因可查，执行被拒。"""
        bj = service_for("backfill_job")
        bj.search_baselines(
            acceptance_job_id, table=acceptance_table,
            start="2026-01-01", end="2026-12-31",
        )
        sync = bj.sync_baseline_plan(
            acceptance_job_id, selection=["ACC-A", "ACC-B"],
            decision_note="组合覆盖",
        )
        assert sync["ok"] is True

        service_for("snapshot_mgr").revoke_coverage("ACC-B", "存储桶不可达")
        executed = bj.execute_backfill(acceptance_job_id)
        assert executed["ok"] is False
        failure = executed["window_failures"][0]
        assert failure["snapshot_id"] == "ACC-B"
        assert "存储桶不可达" in failure["reason"]
        # 原选单保留，清单状态标记失效
        plan = store.backfill_plan(acceptance_job_id)
        assert plan["status"] == "invalid"
        assert [s["snapshot_id"] for s in plan["selection"]] == ["ACC-A", "ACC-B"]
        assert plan["window_failures"][0]["snapshot_id"] == "ACC-B"
        entry = store.find("backfill_job", acceptance_job_id)
        assert entry["status"] == "已失败"
        # 复核接口同样返回失败原因
        review = bj.review_baseline(acceptance_job_id)
        assert review["plan"]["window_failures"][0]["reason"]

    def test_sync_rejects_incomplete_combination(self, acceptance_table, acceptance_job_id):
        bj = service_for("backfill_job")
        result = bj.sync_baseline_plan(
            acceptance_job_id, selection=["ACC-A"],
            decision_note="只选上半年",
        )
        assert result["ok"] is False
        assert result["uncovered_windows"]
        # 不可读快照不能进入清单
        result2 = bj.sync_baseline_plan(
            acceptance_job_id, selection=["ACC-A", "ACC-C"],
        )
        assert result2["ok"] is False
        assert result2["window_failures"][0]["snapshot_id"] == "ACC-C"


class TestSnapshotSearchPureLogic:
    def test_monthly_windows_split_with_readable_label(self):
        windows = snapshot_search.monthly_windows("2026-01-15", "2026-03-10")
        assert [w.label for w in windows] == ["2026年1月", "2026年2月", "2026年3月"]
        assert windows[0].start == "2026-01-15"
        assert windows[-1].end == "2026-03-10"
        assert "2026年1月" in windows[0].readable()

    def test_overlapping_ranges_merge_before_coverage_check(self):
        entry = {"coverage": [
            {"start": "2026-01-01", "end": "2026-03-31"},
            {"start": "2026-03-15", "end": "2026-06-30"},
        ]}
        merged = snapshot_search.merge_ranges(snapshot_search.snapshot_ranges(entry))
        assert merged == [(snapshot_search.date(2026, 1, 1),
                           snapshot_search.date(2026, 6, 30))]
        win = snapshot_search.Window("2026-01-01", "2026-06-30", "上半年")
        state, ratio = snapshot_search.window_cover_state(win, merged)
        assert state == "full" and ratio == 1.0

    def test_handoff_points_deduped(self):
        selections = [
            {"snapshot_id": "X", "coverage": [
                {"start": "2026-01-01", "end": "2026-02-01"},
                {"start": "2026-02-02", "end": "2026-03-31"},
            ]},
            {"snapshot_id": "Y", "coverage": [
                {"start": "2026-04-01", "end": "2026-06-30"}]},
        ]
        points = snapshot_search.handoff_points(selections)
        assert points == ["2026-03-31"]

    def test_revoked_snapshot_excluded(self):
        result = snapshot_search.search_snapshots(
            [{"snapshot_id": "R1", "table": "t", "version": "v1",
              "format": "parquet", "readable": True, "revoked": True,
              "coverage": [{"start": "2026-01-01", "end": "2026-01-31"}],
              "aliases": [], "registered_at": 1}],
            table="t",
            windows=[snapshot_search.Window("2026-01-01", "2026-01-31", "1月")],
        )
        assert result["excluded_snapshots"][0]["reason_code"] == "revoked"
        assert result["ranked_snapshots"] == []
