import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---------------- 回填基准：覆盖注册表检索、清单同步、执行与复核 ----------------

async function backfillPost<T>(path: string, body: unknown = {}): Promise<T> {
  const response = await request(`/api/backfill-jobs${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    throw new Error(extractError(payload))
  }
  return payload as T
}

async function backfillGet<T>(path: string): Promise<T> {
  const response = await request(`/api/backfill-jobs${path}`)
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    throw new Error(extractError(payload))
  }
  return payload as T
}

function extractError(payload: { detail?: unknown; reason?: string }): string {
  const detail = payload?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'reason' in detail) {
    return String((detail as { reason: unknown }).reason)
  }
  return payload?.reason ?? '回填基准操作失败'
}

export type BaselineSearchPayload = {
  table: string
  start?: string
  end?: string
  windows?: { start: string; end: string; label?: string }[]
  format_filter?: string
  readable_only?: boolean
}

export type BaselineSearchResult = {
  ok: boolean
  query_id: string
  table: string
  windows: { start: string; end: string; label: string; readable: string }[]
  ranked_snapshots: {
    snapshot_id: string
    version: string
    format: string
    readable: boolean
    full_count: number
    coverage_ratio: number
    full_windows: string[]
    partial_windows: { window: string; ratio: number }[]
    uncovered_windows: string[]
    boundary_corrected: boolean
    registered_at: number
  }[]
  partial_snapshots: BaselineSearchResult['ranked_snapshots']
  excluded_snapshots: { snapshot_id: string; version: string; reason: string; reason_code: string }[]
  suggested_selection: string[]
  suggested_handoff_points: string[]
  suggested_handoff_count: number
  uncovered_windows: string[]
  full_coverage_available: boolean
}

export type BaselinePlan = {
  job_id: number
  table: string
  windows: { start: string; end: string; label: string; readable: string }[]
  selection: {
    snapshot_id: string
    version: string
    format: string
    coverage: { start: string; end: string }[]
    boundary_keys: Record<string, string>
    boundary_corrected: boolean
  }[]
  handoff_points: string[]
  handoff_count: number
  decision_mode: string
  decision_note: string
  status: string
  executed: boolean
  window_failures: { snapshot_id: string; reason: string; window: unknown }[]
  synced_at: number
}

export type BackfillReview = {
  ok: boolean
  job_id: number
  entry_brief: Record<string, string>
  plan: BaselinePlan | null
  query_history: { query_id: string; queried_at: number; table: string }[]
  executions: {
    ok: boolean
    actual_versions?: { snapshot_id: string; version: string }[]
    boundary_key_actual?: {
      snapshot_id: string
      version: string
      boundary_corrected: boolean
      boundary_keys: Record<string, string>
    }[]
    selection_rationale?: string
    decision_mode?: string
    handoff_points?: string[]
    reason?: string
    window_failures?: { snapshot_id: string; reason: string }[]
    executed_at: number
  }[]
}

export async function searchBaselines(
  jobId: number,
  payload: BaselineSearchPayload,
): Promise<BaselineSearchResult> {
  return backfillPost(`/${jobId}/baseline-search`, payload)
}

export async function syncBaselinePlan(
  jobId: number,
  payload: {
    selection: string[]
    source_query_id?: string
    decision_mode?: string
    decision_note?: string
  },
): Promise<{ ok: boolean; plan: BaselinePlan }> {
  return backfillPost(`/${jobId}/baseline-plan`, payload)
}

export async function executeBackfill(jobId: number): Promise<Record<string, unknown>> {
  return backfillPost(`/${jobId}/execute`)
}

export async function reviewBackfill(jobId: number): Promise<BackfillReview> {
  return backfillGet(`/${jobId}/review`)
}

export async function getBaselinePlan(jobId: number): Promise<BackfillReview> {
  return backfillGet(`/${jobId}/baseline-plan`)
}

export async function getBaselineQuery(
  queryId: string,
): Promise<{ ok: boolean; query: BaselineSearchResult & { queried_at: number } }> {
  return backfillGet(`/baseline-queries/${queryId}`)
}

export type CoverageRecord = {
  snapshot_id: string
  table: string
  version: string
  aliases: string[]
  format: string
  readable: boolean
  coverage: { start: string; end: string }[]
  boundary_keys: Record<string, string>
  boundary_corrected: boolean
  revoked: boolean
  revoke_reason: string
}

export async function listSnapshotRegistry(table?: string): Promise<{ items: CoverageRecord[]; total: number }> {
  const query = table ? `?table=${encodeURIComponent(table)}` : ''
  const response = await request(`/api/backfill-jobs/snapshot-registry${query}`)
  return await response.json()
}

export async function registerSnapshotCoverage(
  payload: Omit<CoverageRecord, 'revoked' | 'revoke_reason'> & { registered_at?: number },
): Promise<{ ok: boolean; record: CoverageRecord }> {
  return backfillPost('/snapshot-registry', payload)
}

export async function revokeSnapshotCoverage(
  snapshotId: string,
  reason: string,
): Promise<{ ok: boolean; record: CoverageRecord }> {
  return backfillPost(`/snapshot-registry/${snapshotId}/revoke`, { reason })
}
