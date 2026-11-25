# 流批一体数据集成平台

面向实时流接入、批流合并、变更捕获与分区治理的流批一体数据集成平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流式数据源 | `stream_source` | 流式源配置 | 源标识、消息中间件、Topic名称 |
| 批次分片 | `batch_slice` | 分片批次 | 批次编号、源表名称、分片键列 |
| 变更捕获 | `change_capture` | 变更捕获配置 | 配置编号、源数据库、目标表 |
| Schema治理 | `schema_govern` | Schema版本 | 主题名称、Schema定义、版本号 |
| 水位管理 | `watermark` | 消费水位 | 分区标识、消费组、当前位点 |
| 迟到数据 | `late_data` | 迟到数据策略 | 策略编号、关联源、允许延迟秒数 |
| 流式关联 | `stream_lookup` | 关联规则 | 规则编号、左流来源、右表来源 |
| 合并策略 | `merge_plan` | 合并计划 | 计划编号、流式来源、批式来源 |
| 分区扩缩 | `partition_scale` | 分区策略 | 策略编号、目标表、分区字段 |
| 快照管理 | `snapshot_mgr` | 数据快照 | 快照编号、源表名称、快照时间 |
| 数据对账 | `data_reconcile` | 对账任务 | 任务编号、左源表、右源表 |
| 回填作业 | `backfill_job` | 回填任务 | 任务编号、目标表、回填起始时间 |
| 冷热分层 | `cold_archive` | 归档策略 | 策略编号、源表名称、热数据天数 |
| 压缩合并 | `compaction` | 压缩任务 | 任务编号、目标表、目标分区 |
| 血缘图 | `lineage` | 血缘关系 | 关系编号、上游表、下游表 |
| 变更日志 | `changelog` | 变更记录 | 记录编号、目标表、变更类型 |
| 保留管理 | `retention` | 保留策略 | 策略编号、目标表、保留天数 |
| 表格式 | `table_format` | 表格式配置 | 配置编号、目标表、表格式类型 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 回填基准检索（backfill_job × snapshot_mgr）

回填登记不再只检查依赖表「有没有快照」，而是按缺失时段的数据窗口依据快照
**实际覆盖范围**寻找基准，相关代码：

- `backend/app/services/snapshot_search.py`：纯领域逻辑——自然月窗口切分（带可读
  标签）、区间并集合并、full/partial/none 覆盖判定、按完整覆盖窗口数排序、
  贪心组合推荐、跨版本交接位置去重。
- 覆盖注册表 `store._snapshot_coverage`：登记每个物理快照的覆盖区间、压缩格式、
  版本别名、边界键与可读状态；撤销只标记失效不删除记录。
- 检索排除原因分四类：`unreadable`（同名不可读旧版本）、`format_mismatch`（格式
  不符筛选）、`alias_ambiguous`（同名/别名对应多个物理快照）、`revoked`（已撤销）；
  只覆盖部分窗口的版本在 `partial_snapshots` 单列。
- 回填清单 `store._backfill_plans`：选定组合同步后冻结交接点，回填执行严格读取
  清单登记的实际版本与边界键；源快照失效时原选单保留、窗口失败原因可查，历次
  查询结论存入 `store._baseline_query_history`，旧结论持续可复核。
- 专用路由前缀 `/api/backfill-jobs`（先于通用 `/api/{module}` 注册）：
  `snapshot-registry`、`{job_id}/baseline-search`、`{job_id}/baseline-plan`、
  `{job_id}/execute`、`{job_id}/review`、`baseline-queries/{query_id}`。
- 前端在回填作业模块挂载 `BackfillBaselinePanel.vue`，承载筛选、排序结果、
  排除原因、清单同步、执行与撤销复核。
- 验收测试：`backend/tests/test_backfill_baseline.py`（区间交叠双快照 + 同名不可读
  旧版本 → 组合选择 → 全段新版本重选 → 执行核对边界键 → 撤销复核）。

领域：数据工程
子方向：数仓与数据湖
