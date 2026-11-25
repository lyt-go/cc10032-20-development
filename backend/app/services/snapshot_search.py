"""回填基准快照检索：按数据窗口的实际覆盖范围寻找快照基准。

领域规则：
1. 缺失时段可以显式给出数据窗口，未给出时按自然月切分；窗口带可读标签。
2. 每个快照登记一个或多个覆盖区间，区间先做并集合并再判定覆盖关系：
   - full：窗口被快照覆盖区间的并集完整包含
   - partial：与窗口交叠但未完整覆盖
   - none：无交叠
3. 候选快照先过资格校验，排除原因单独保留：
   - 不可读（readable=False）
   - 压缩格式不符合筛选
   - 版本别名歧义（同一表同一版本别名存在两个及以上可读候选，无法定位物理快照）
   - 已撤销
4. 排序按「完整覆盖窗口数 → 覆盖率 → 登记时间」，只覆盖部分窗口的版本单列。
5. 组合推荐用贪心集合覆盖；跨版本交接位置按边界日期去重，只算一次。

本模块只依赖标准库，纯函数便于单测。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

# 与 snapshot_mgr 服务保持一致的可读压缩格式集合
READABLE_FORMATS = ("parquet", "orc", "avro")


@dataclass(frozen=True)
class Window:
    start: str
    end: str
    label: str = ""

    def readable(self) -> str:
        if self.label:
            return f"{self.label}（{self.start} ~ {self.end}）"
        return f"{self.start} ~ {self.end}"


def parse_day(value: str) -> date:
    return date.fromisoformat(str(value).strip()[:10])


def days_between(start: date, end: date) -> int:
    """闭区间天数。"""
    return (end - start).days + 1


def monthly_windows(start: str, end: str) -> list[Window]:
    """把缺失时段按自然月切成数据窗口，首尾窗口保留原始边界。"""
    start_day, end_day = parse_day(start), parse_day(end)
    if end_day < start_day:
        raise ValueError("缺失时段结束时间早于起始时间")
    windows: list[Window] = []
    cursor = date(start_day.year, start_day.month, 1)
    while cursor <= end_day:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        month_end = next_month - timedelta(days=1)
        win_start = max(start_day, cursor)
        win_end = min(end_day, month_end)
        windows.append(Window(
            start=win_start.isoformat(),
            end=win_end.isoformat(),
            label=f"{win_start.year}年{win_start.month}月",
        ))
        cursor = next_month
    return windows


def normalize_windows(raw_windows: list[dict[str, Any]] | None,
                      start: str | None, end: str | None) -> list[Window]:
    if raw_windows:
        windows = [
            Window(str(w["start"]), str(w["end"]), str(w.get("label", "")))
            for w in raw_windows
        ]
    elif start and end:
        windows = monthly_windows(start, end)
    else:
        raise ValueError("必须提供缺失时段或显式数据窗口")
    for win in windows:
        if parse_day(win.end) < parse_day(win.start):
            raise ValueError(f"窗口 {win.readable()} 结束早于起始")
    return windows


def merge_ranges(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """区间并集合并，重叠与相接的区间合并为一段。"""
    ordered = sorted((s, e) for s, e in ranges if e >= s)
    merged: list[tuple[date, date]] = []
    for start_day, end_day in ordered:
        if not merged or start_day > merged[-1][1] + timedelta(days=1):
            merged.append((start_day, end_day))
        else:
            prev_start, prev_end = merged[-1]
            merged[-1] = (prev_start, max(prev_end, end_day))
    return merged


def snapshot_ranges(entry: dict[str, Any]) -> list[tuple[date, date]]:
    ranges = []
    for cov in entry.get("coverage", []) or []:
        ranges.append((parse_day(cov["start"]), parse_day(cov["end"])))
    return ranges


def window_cover_state(window: Window,
                       merged: list[tuple[date, date]]) -> tuple[str, float]:
    """返回 (full|partial|none, 覆盖率)。"""
    win_start, win_end = parse_day(window.start), parse_day(window.end)
    total = days_between(win_start, win_end)
    covered = 0
    for range_start, range_end in merged:
        overlap_start = max(win_start, range_start)
        overlap_end = min(win_end, range_end)
        if overlap_end >= overlap_start:
            covered += days_between(overlap_start, overlap_end)
    if covered == 0:
        return "none", 0.0
    ratio = round(covered / total, 4)
    if covered >= total:
        return "full", 1.0
    return "partial", ratio


def handoff_points(selections: list[dict[str, Any]]) -> list[str]:
    """计算跨版本交接位置，同一日期只算一次。

    selections: [{"snapshot_id", "coverage": [{"start","end"}]}]
    每个版本取其合并后的首段起点排序，版本切换处的边界日期记为一个交接位置。
    """
    version_ranges: list[tuple[date, date, str]] = []
    for sel in selections:
        merged = merge_ranges(snapshot_ranges(sel))
        if not merged:
            continue
        first_start, first_end = merged[0]
        version_ranges.append((first_start, first_end, str(sel["snapshot_id"])))
    version_ranges.sort(key=lambda item: (item[0], item[1]))
    points: set[str] = set()
    for prev, nxt in zip(version_ranges, version_ranges[1:]):
        if prev[2] == nxt[2]:
            continue
        # 从上一版本切到下一版本的位置：上一版本首段的结束边界
        points.add(prev[1].isoformat())
    return sorted(points)


def _coverage_view(entry: dict[str, Any], windows: list[Window]) -> dict[str, Any]:
    merged = merge_ranges(snapshot_ranges(entry))
    full, partial, none_win = [], [], []
    ratio_sum = 0.0
    for win in windows:
        state, ratio = window_cover_state(win, merged)
        ratio_sum += ratio
        if state == "full":
            full.append(win.readable())
        elif state == "partial":
            partial.append({"window": win.readable(), "ratio": ratio})
        else:
            none_win.append(win.readable())
    return {
        "full_windows": full,
        "partial_windows": partial,
        "uncovered_windows": none_win,
        "full_count": len(full),
        "coverage_ratio": round(ratio_sum / max(len(windows), 1), 4),
        "merged_ranges": [
            {"start": s.isoformat(), "end": e.isoformat()} for s, e in merged
        ],
    }


def _alias_ambiguity(entries: list[dict[str, Any]]) -> dict[str, list[str]]:
    """返回 snapshot_id -> 歧义原因。同一表同一别名出现多个可读物理快照即歧义。"""
    alias_owners: dict[str, list[str]] = {}
    for entry in entries:
        names = [str(entry.get("version", ""))] + [str(a) for a in entry.get("aliases", [])]
        for name in set(n for n in names if n):
            alias_owners.setdefault(name, []).append(str(entry["snapshot_id"]))
    ambiguous: dict[str, list[str]] = {}
    for name, owners in alias_owners.items():
        if len(set(owners)) > 1:
            for owner in owners:
                ambiguous.setdefault(owner, []).append(name)
    return ambiguous


def search_snapshots(registry: list[dict[str, Any]], *, table: str,
                     windows: list[Window], format_filter: str | None = None,
                     readable_only: bool = True) -> dict[str, Any]:
    """按目标表 + 数据窗口检索快照，返回排序候选、部分覆盖、排除清单与推荐组合。"""
    scoped = [e for e in registry if str(e.get("table", "")) == table]
    excluded: list[dict[str, Any]] = []
    passing: list[dict[str, Any]] = []

    for entry in scoped:
        snapshot_id = str(entry["snapshot_id"])
        if entry.get("revoked"):
            excluded.append({
                "snapshot_id": snapshot_id,
                "version": entry.get("version", ""),
                "reason": f"快照已撤销：{entry.get('revoke_reason', '未注明原因')}",
                "reason_code": "revoked",
            })
            continue
        if readable_only and not entry.get("readable", True):
            excluded.append({
                "snapshot_id": snapshot_id,
                "version": entry.get("version", ""),
                "reason": "快照不可读，无法作为回填基准",
                "reason_code": "unreadable",
            })
            continue
        fmt = str(entry.get("format", ""))
        if format_filter and fmt != format_filter:
            excluded.append({
                "snapshot_id": snapshot_id,
                "version": entry.get("version", ""),
                "reason": f"压缩格式 {fmt or '未知'} 不符合筛选格式 {format_filter}",
                "reason_code": "format_mismatch",
            })
            continue
        passing.append(entry)

    # 别名歧义只在仍有资格的可读候选之间判定
    ambiguous = _alias_ambiguity(passing)
    eligible: list[dict[str, Any]] = []
    for entry in passing:
        snapshot_id = str(entry["snapshot_id"])
        if snapshot_id in ambiguous:
            excluded.append({
                "snapshot_id": snapshot_id,
                "version": entry.get("version", ""),
                "reason": f"版本别名 {','.join(sorted(set(ambiguous[snapshot_id])))} 存在多个同名物理快照，定位歧义",
                "reason_code": "alias_ambiguous",
            })
            continue
        eligible.append(entry)

    ranked: list[dict[str, Any]] = []
    partial_only: list[dict[str, Any]] = []
    for entry in eligible:
        view = _coverage_view(entry, windows)
        payload = {
            "snapshot_id": str(entry["snapshot_id"]),
            "version": entry.get("version", ""),
            "format": entry.get("format", ""),
            "readable": entry.get("readable", True),
            "registered_at": entry.get("registered_at", 0),
            "boundary_keys": entry.get("boundary_keys", {}),
            "boundary_corrected": entry.get("boundary_corrected", False),
            **view,
        }
        if view["full_count"] == 0 and (view["partial_windows"]):
            partial_only.append(payload)
        else:
            ranked.append(payload)

    ranked.sort(key=lambda item: (
        -item["full_count"], -item["coverage_ratio"], -item["registered_at"],
        item["snapshot_id"],
    ))
    partial_only.sort(key=lambda item: (-item["coverage_ratio"], item["snapshot_id"]))

    suggested, uncovered = _suggest_combination(eligible, windows)
    handoff = handoff_points([
        {"snapshot_id": sid, "coverage": next(
            e.get("coverage", []) for e in eligible if str(e["snapshot_id"]) == sid
        )}
        for sid in suggested
    ])
    return {
        "table": table,
        "windows": [
            {"start": w.start, "end": w.end, "label": w.label, "readable": w.readable()}
            for w in windows
        ],
        "format_filter": format_filter,
        "readable_only": readable_only,
        "ranked_snapshots": ranked,
        "partial_snapshots": partial_only,
        "excluded_snapshots": excluded,
        "suggested_selection": suggested,
        "suggested_handoff_points": handoff,
        "suggested_handoff_count": len(handoff),
        "uncovered_windows": uncovered,
        "full_coverage_available": bool(ranked and ranked[0]["full_count"] == len(windows)),
    }


def _suggest_combination(eligible: list[dict[str, Any]],
                         windows: list[Window]) -> tuple[list[str], list[str]]:
    """贪心集合覆盖：每轮选完整覆盖剩余窗口最多的快照。"""
    views = {
        str(e["snapshot_id"]): _coverage_view(e, windows) for e in eligible
    }
    remaining = {w.readable() for w in windows}
    chosen: list[str] = []
    while remaining:
        best_id, best_covers = None, set()
        for snapshot_id, view in views.items():
            if snapshot_id in chosen:
                continue
            covers = remaining & set(view["full_windows"])
            if len(covers) > len(best_covers):
                best_id, best_covers = snapshot_id, covers
        if best_id is None:
            break
        chosen.append(best_id)
        remaining -= best_covers
    return chosen, sorted(remaining)
