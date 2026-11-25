<template>
  <section class="baseline-panel" data-testid="baseline-panel">
    <h3>回填基准检索与清单</h3>
    <p class="page-desc">
      按目标表、缺失时段与可读格式筛选快照，按覆盖数据窗口排序；部分覆盖与因格式、别名歧义、不可读被排除的版本分别展示，
      选定组合同步到回填清单，跨版本交接位置只算一次。
    </p>

    <div class="baseline-toolbar">
      <label>
        <span>回填任务</span>
        <select v-model="selectedJobId" data-testid="baseline-job-select" @change="onJobChange">
          <option v-for="job in jobs" :key="job.id" :value="Number(job.id)">
            {{ job['任务编号'] }}｜{{ job['目标表'] }}｜{{ job.status }}
          </option>
        </select>
      </label>
      <button class="btn" type="button" data-testid="baseline-refresh" @click="loadJobs">刷新任务</button>
      <button class="btn" type="button" data-testid="baseline-review" @click="loadReview">复核该次记录</button>
    </div>

    <form class="analysis-form" @submit.prevent="runSearch">
      <label>
        目标表
        <input v-model="searchForm.table" data-testid="search-table" placeholder="例如 dwd_orders_merged" required />
      </label>
      <label>
        缺失起始
        <input v-model="searchForm.start" data-testid="search-start" placeholder="2026-01-01" />
      </label>
      <label>
        缺失结束
        <input v-model="searchForm.end" data-testid="search-end" placeholder="2026-06-30" />
      </label>
      <label>
        压缩格式
        <select v-model="searchForm.format_filter" data-testid="search-format">
          <option value="">不限</option>
          <option v-for="fmt in formats" :key="fmt" :value="fmt">{{ fmt }}</option>
        </select>
      </label>
      <label>
        可读性
        <select v-model="searchForm.readable_only" data-testid="search-readable">
          <option :value="true">仅可读快照</option>
          <option :value="false">包含不可读</option>
        </select>
      </label>
      <button class="btn primary" data-testid="search-submit" type="submit">检索基准</button>
    </form>

    <div v-if="searchResult" class="baseline-result">
      <div class="baseline-section-head">
        <strong>
          检索结果（查询编号 {{ searchResult.query_id }}）：
          {{ searchResult.ranked_snapshots.length }} 个有效候选，
          {{ searchResult.partial_snapshots.length }} 个仅部分覆盖，
          {{ searchResult.excluded_snapshots.length }} 个被排除
        </strong>
        <span v-if="searchResult.full_coverage_available" class="tag tag-ok">存在单版本全覆盖</span>
        <span v-else class="tag tag-warn">需组合覆盖</span>
      </div>

      <p class="window-line">
        数据窗口：
        <span v-for="win in searchResult.windows" :key="win.start" class="tag">{{ win.readable }}</span>
      </p>
      <p class="window-line">
        推荐组合：
        <span v-for="sid in searchResult.suggested_selection" :key="sid" class="tag tag-ok">{{ sid }}</span>
        交接位置（去重）：
        <span v-for="point in searchResult.suggested_handoff_points" :key="point" class="tag tag-warn">{{ point }}</span>
        <template v-if="!searchResult.suggested_handoff_points.length">
          <span class="tag tag-ok">无跨版本交接</span>
        </template>
      </p>

      <table class="data-table">
        <thead>
          <tr>
            <th>选择</th>
            <th>快照编号/版本</th>
            <th>格式</th>
            <th>完整覆盖窗口数</th>
            <th>覆盖率</th>
            <th>覆盖窗口</th>
            <th>部分覆盖</th>
            <th>边界键</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in searchResult.ranked_snapshots" :key="item.snapshot_id" data-testid="ranked-row">
            <td><input v-model="pickedIds" type="checkbox" :value="item.snapshot_id" /></td>
            <td>{{ item.snapshot_id }}（{{ item.version }}）</td>
            <td>{{ item.format }}</td>
            <td>{{ item.full_count }} / {{ searchResult.windows.length }}</td>
            <td>{{ (item.coverage_ratio * 100).toFixed(1) }}%</td>
            <td>{{ item.full_windows.join('；') || '—' }}</td>
            <td>
              <span v-for="p in item.partial_windows" :key="p.window">
                {{ p.window }} {{ (p.ratio * 100).toFixed(0) }}%；
              </span>
              <span v-if="!item.partial_windows.length">—</span>
            </td>
            <td>
              <span v-if="item.boundary_corrected" class="tag tag-ok">已更正</span>
              <span v-else>原值</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="searchResult.partial_snapshots.length" class="baseline-block">
        <h4>仅覆盖部分窗口的版本（不能单独作为基准）</h4>
        <table class="data-table">
          <thead>
            <tr><th>快照编号/版本</th><th>格式</th><th>部分覆盖窗口</th><th>覆盖率</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in searchResult.partial_snapshots" :key="item.snapshot_id">
              <td>{{ item.snapshot_id }}（{{ item.version }}）</td>
              <td>{{ item.format }}</td>
              <td>{{ item.partial_windows.map((p) => p.window).join('；') }}</td>
              <td>{{ (item.coverage_ratio * 100).toFixed(1) }}%</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="searchResult.excluded_snapshots.length" class="baseline-block">
        <h4>被排除的版本及原因</h4>
        <table class="data-table">
          <thead>
            <tr><th>快照编号/版本</th><th>排除原因编码</th><th>可读原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in searchResult.excluded_snapshots" :key="item.snapshot_id" data-testid="excluded-row">
              <td>{{ item.snapshot_id }}（{{ item.version }}）</td>
              <td>{{ item.reason_code }}</td>
              <td>{{ item.reason }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="baseline-actions">
        <label class="decision-note">
          <span>选择依据（同步到清单）</span>
          <input v-model="decisionNote" data-testid="decision-note" placeholder="说明采用组合或新版本的理由" />
        </label>
        <button class="btn primary" data-testid="sync-plan" type="button" @click="syncPlan">
          同步选定结果到回填清单
        </button>
        <button class="btn" data-testid="apply-suggested" type="button" @click="applySuggested">
          采用推荐组合
        </button>
        <button class="btn primary" data-testid="execute-backfill" type="button" @click="runExecute">
          按清单实际版本执行回填
        </button>
      </div>
    </div>

    <div v-if="review" class="baseline-block">
      <h4>复核：清单 / 执行记录 / 历史查询结论</h4>
      <p v-if="review.plan" class="window-line">
        清单状态：<span class="tag" :class="review.plan.status === 'invalid' ? 'tag-danger' : 'tag-ok'">{{ review.plan.status }}</span>
        实际版本：
        <span v-for="sel in review.plan.selection" :key="sel.snapshot_id" class="tag">
          {{ sel.snapshot_id }}（{{ sel.version }}）
        </span>
        决策：{{ review.plan.decision_mode }}｜{{ review.plan.decision_note || '—' }}
      </p>
      <p v-if="review.plan?.window_failures?.length" class="error-text">
        窗口失败原因：
        <span v-for="f in review.plan.window_failures" :key="f.snapshot_id">
          {{ f.snapshot_id }}：{{ f.reason }}；
        </span>
      </p>
      <table v-if="review.executions.length" class="data-table">
        <thead>
          <tr><th>执行结果</th><th>实际版本</th><th>边界键实际值</th><th>选择依据</th><th>交接点</th></tr>
        </thead>
        <tbody>
          <tr v-for="(exe, idx) in review.executions" :key="idx">
            <td>
              <span :class="exe.ok ? 'tag tag-ok' : 'tag tag-danger'">{{ exe.ok ? '成功' : '失败' }}</span>
            </td>
            <td>
              <span v-for="v in exe.actual_versions" :key="v.snapshot_id">
                {{ v.snapshot_id }}（{{ v.version }}）；
              </span>
              <span v-if="!exe.actual_versions">—</span>
            </td>
            <td>
              <div v-for="b in exe.boundary_key_actual" :key="b.snapshot_id">
                {{ b.snapshot_id }}
                <span v-if="b.boundary_corrected" class="tag tag-ok">已更正</span>
                ：{{ JSON.stringify(b.boundary_keys) }}
              </div>
              <span v-if="!exe.boundary_key_actual">—</span>
            </td>
            <td>{{ exe.selection_rationale || exe.reason || '—' }}</td>
            <td>{{ (exe.handoff_points || []).join('、') || '—' }}</td>
          </tr>
        </tbody>
      </table>
      <p>
        历史查询结论（{{ review.query_history.length }} 条，旧结论持续可查）：
        <button
          v-for="q in review.query_history"
          :key="q.query_id"
          class="link"
          type="button"
          @click="inspectQuery(q.query_id)"
        >
          {{ q.query_id }}
        </button>
      </p>
      <pre v-if="archivedQuery" class="query-archive" data-testid="archived-query">{{ archivedQuery }}</pre>
    </div>

    <div class="baseline-block">
      <h4>快照覆盖注册表（撤销后清单原选单与失败原因保留）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>快照编号</th><th>目标表</th><th>版本</th><th>别名</th><th>格式</th>
            <th>可读</th><th>覆盖区间</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="rec in registry" :key="rec.snapshot_id">
            <td>{{ rec.snapshot_id }}</td>
            <td>{{ rec.table }}</td>
            <td>{{ rec.version }}</td>
            <td>{{ rec.aliases.join('、') || '—' }}</td>
            <td>{{ rec.format }}</td>
            <td>{{ rec.readable ? '是' : '否' }}</td>
            <td>
              <span v-for="c in rec.coverage" :key="c.start">{{ c.start }}~{{ c.end }}；</span>
            </td>
            <td>
              <span v-if="rec.revoked" class="tag tag-danger">已撤销：{{ rec.revoke_reason }}</span>
              <span v-else class="tag tag-ok">有效</span>
            </td>
            <td>
              <button
                v-if="!rec.revoked"
                class="link"
                type="button"
                :data-testid="`revoke-${rec.snapshot_id}`"
                @click="revoke(rec.snapshot_id)"
              >
                撤销
              </button>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="message" class="page-foot">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="baseline-error">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  type BaselineSearchResult,
  type CoverageRecord,
  type BackfillReview,
  executeBackfill,
  getBaselineQuery,
  listEntries,
  listSnapshotRegistry,
  reviewBackfill,
  revokeSnapshotCoverage,
  searchBaselines,
  syncBaselinePlan,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const formats = ['parquet', 'orc', 'avro']
const jobs = ref<EntryRow[]>([])
const selectedJobId = ref<number>(0)
const registry = ref<CoverageRecord[]>([])
const searchResult = ref<BaselineSearchResult | null>(null)
const review = ref<BackfillReview | null>(null)
const pickedIds = ref<string[]>([])
const decisionNote = ref('')
const message = ref('')
const errorMessage = ref('')
const archivedQuery = ref('')

const searchForm = reactive({
  table: '',
  start: '',
  end: '',
  format_filter: '',
  readable_only: true,
})

async function loadJobs() {
  errorMessage.value = ''
  try {
    const page = await listEntries('backfill_job')
    jobs.value = page.items
    if (!selectedJobId.value && jobs.value.length) {
      selectedJobId.value = Number(jobs.value[0].id)
      onJobChange()
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回填任务读取失败'
  }
}

async function loadRegistry() {
  const payload = await listSnapshotRegistry()
  registry.value = payload.items
}

function onJobChange() {
  const job = jobs.value.find((item) => Number(item.id) === selectedJobId.value)
  if (!job) return
  searchForm.table = String(job['目标表'] ?? '')
  searchForm.start = String(job['回填起始时间'] ?? '')
  searchForm.end = String(job['回填结束时间'] ?? '')
  review.value = null
  archivedQuery.value = ''
}

async function runSearch() {
  errorMessage.value = ''
  message.value = ''
  try {
    searchResult.value = await searchBaselines(selectedJobId.value, {
      table: searchForm.table,
      start: searchForm.start || undefined,
      end: searchForm.end || undefined,
      format_filter: searchForm.format_filter || undefined,
      readable_only: searchForm.readable_only,
    })
    pickedIds.value = [...searchResult.value.suggested_selection]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基准检索失败'
  }
}

function applySuggested() {
  if (!searchResult.value) return
  pickedIds.value = [...searchResult.value.suggested_selection]
}

async function syncPlan() {
  errorMessage.value = ''
  if (!pickedIds.value.length) {
    errorMessage.value = '请先勾选至少一个快照'
    return
  }
  try {
    const result = await syncBaselinePlan(selectedJobId.value, {
      selection: pickedIds.value,
      source_query_id: searchResult.value?.query_id,
      decision_mode: pickedIds.value.length === 1 ? 'single' : 'combination',
      decision_note: decisionNote.value,
    })
    message.value = `清单已同步：${result.plan.selection.length} 个实际版本，交接点 ${result.plan.handoff_count} 个`
    await loadJobs()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单同步失败'
  }
}

async function runExecute() {
  errorMessage.value = ''
  try {
    const result = await executeBackfill(selectedJobId.value)
    message.value = `回填已开始，实际版本：${JSON.stringify(result.actual_versions ?? result)}`
    await loadJobs()
    await loadReview()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回填执行失败'
  }
}

async function loadReview() {
  errorMessage.value = ''
  try {
    review.value = await reviewBackfill(selectedJobId.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复核读取失败'
  }
}

async function inspectQuery(queryId: string) {
  const payload = await getBaselineQuery(queryId)
  archivedQuery.value = JSON.stringify(payload.query, null, 2)
}

async function revoke(snapshotId: string) {
  errorMessage.value = ''
  const reason = window.prompt(`撤销快照 ${snapshotId} 的原因`, '底层存储失效')
  if (!reason) return
  try {
    await revokeSnapshotCoverage(snapshotId, reason)
    message.value = `快照 ${snapshotId} 已撤销，原选单与窗口失败原因已保留`
    await loadRegistry()
    await loadReview()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '撤销失败'
  }
}

defineExpose({ runExecute, loadJobs, loadRegistry })

onMounted(async () => {
  await loadJobs()
  await loadRegistry()
})
</script>

<style scoped>
.baseline-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  margin: 16px 0;
}
.baseline-toolbar {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.baseline-toolbar label span,
.decision-note span {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 4px;
}
.baseline-toolbar select,
.decision-note input {
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.baseline-result {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  margin-top: 12px;
}
.baseline-section-head {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.baseline-block {
  margin-top: 14px;
}
.baseline-block h4 {
  margin: 8px 0;
}
.baseline-actions {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  margin-top: 12px;
  flex-wrap: wrap;
}
.decision-note {
  flex: 1;
  min-width: 260px;
}
.decision-note input {
  width: 100%;
}
.window-line {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
  font-size: 13px;
}
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef2f7;
  font-size: 12px;
}
.tag-ok {
  background: #dcfce7;
  color: #166534;
}
.tag-warn {
  background: #fef9c3;
  color: #854d0e;
}
.tag-danger {
  background: #fee2e2;
  color: #991b1b;
}
.query-archive {
  background: #1e293b;
  color: #e2e8f0;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  max-height: 240px;
  overflow: auto;
}
</style>
