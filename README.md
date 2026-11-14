# schemaevo · Parquet 字段 ID 与类型演进映射

湖仓表治理小工具：上游提交 Parquet 表 schema 变更时，先算清「字段怎么变了、兼容级别是什么、迁移 SQL 按什么顺序执行、谁会读坏」。

## 能力

- 字段匹配：`field_id` 优先，无 id 时按名称精确匹配；改名、字段 ID 变更、新增、删除逐条标注依据。
- 类型提升矩阵：代码内显式表（int8/int16/int32/int64、float/double、decimal 精度、string→int、date→timestamp 等），表外类型对一律 `unknown`（不猜）。
- 兼容级别：按「旧写新读 / 新写旧读」双向判定，输出 `full` / `backward` / `forward` / `none`。
- 迁移计划：有序 `ALTER TABLE` 序列（改名 → 加列/默认值 → 回填 → 类型变更 → 删列最后）、需回填对象、读写方升级顺序；分区字段被删或改类型直接阻断。
- 滚动切换窗口：按任务清单（当前版本 / 读写类型 / 切换时刻）切出阶段，逐阶段核对该窗口内仍在运行的所有写入版本 × 读取版本；发现不兼容组合就停在该阶段，后续阶段标记未推进。
- 阶段执行状态：可逐步推进/重试；失败保留原状态与已完成步骤，重试同一阶段不会重复推进，已完成阶段再次提交直接返回 `already_completed`。
- 列引用检查：数据文件清单里引用被删/被阻断列的 → `broken_consumers`；引用改名列的 → 需同步改名。
- 4 个页面：首页（场景 + 三组样例 + 演示步骤）、对比工作台、结果页、复核与导出（历史 + SQL 下载 + 矩阵说明）。结果页、历史列表与下载的 `.sql` 展示同一阶段、阻断组合与已完成步骤。
- 运行记录以 JSON 落在 `/app/data/runs/`（compose 命名卷），同一输入可反复重跑，历史可回看。

## 核心规则（纯函数模块）

| 模块 | 文件 | 规则 |
|---|---|---|
| 字段匹配 | `app/matching.py` | id 相同优先；id 同名称变 → renamed；名称同 id 变 → id_changed（人工确认）；无匹配 → added/removed |
| 类型矩阵 | `app/types.py` | 显式提升/阻断表；decimal 精度提升兼容、收窄阻断、标度变更 unknown |
| 兼容判定 | `app/compat.py` | 双向各自判定后合成级别；分区字段增删改类型双向阻断 |
| 迁移计划 | `app/migration.py` | 固定顺序出 ALTER，阻断项不产出 ALTER，删列最后 |
| 滚动切换 | `app/rollout.py` | 按 switch_at 切阶段，逐阶段核对在跑写入 × 读取版本；首个不兼容阶段停步；阶段状态机可推进/重试且幂等 |
| 引用检查 | `app/consumers.py` | 逐文件逐列比对匹配结果，输出 broken / needs_update / unknown |
| 报表编排 | `app/report.py` | 汇总成可重放的运行记录 + 下载脚本 |

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18216/health
```

页面：<http://127.0.0.1:18216/>

## 演示步骤

1. 首页点「用样例会 B 执行对比」，工作台预填 `dwd_payment` 旧/新 schema、数据文件清单与任务清单。
2. 点「执行对比」→ 结果页：改名 1、新增 2、回填 1、`decimal(10,2)→(18,2)`，兼容级别 `backward`，ALTER 4 条；滚动窗口阶段 1..2 安全、阶段 3 因「新写 × 旧读」阻断、阶段 4 未推进。
3. 在结果页「滚动切换窗口」分段点「执行阶段 1 / 2」，到阶段 3 时按钮变为「重试阶段 3」：失败后仍停在阶段 3，已完成步骤保持 1、2，重试只增加尝试次数。
4. 换成样例 C（`dwd_shipment`）→ 级别 `none`、3 条阻断原因、`broken_consumers` 若干、`can_execute=false`；滚动窗口在阶段 2 即阻断。
5. 到「复核与导出」看历史（同一阶段/阻断组合/已完成步骤）、下载 `*.sql`（脚本尾部附带同一份切换进度）；同一输入可重复执行。

样例：

| 样例 | 表 | 预期 |
|---|---|---|
| A | `dwd_orders` | `full`，0 条迁移 SQL，4 个阶段全部安全 |
| B | `dwd_payment` | `backward`，rename 1 / added 2 / 回填 1 / ALTER 4；阶段 3 新写旧读阻断 |
| C | `dwd_shipment` | `none`，3 条阻断 + broken_consumers；阶段 2 旧写新读阻断 |

### 任务清单（滚动切换输入）

```json
{
  "tasks": [
    {"name": "reader_dashboard", "role": "reader", "version": "old",
     "switch_at": "2026-11-20T02:00:00+08:00"},
    {"name": "writer_payment_hourly", "role": "writer", "version": "old",
     "switch_at": "2026-11-20T03:00:00+08:00"},
    {"name": "reader_archive", "role": "reader", "version": "old",
     "start_at": "2026-11-20T00:00:00+08:00", "stop_at": "2026-11-20T06:00:00+08:00"}
  ]
}
```

- `role`：`writer` 或 `reader`；`version`：任务当前所在的 `old` / `new`。
- `switch_at`：`version=old` 的任务切到新版本的时刻；同刻多条变更合并为一个阶段。`version=new` 的任务用 `start_at` 表达启用时刻。
- `start_at` / `stop_at` 可选；停掉的任务不参与后续窗口。
- 核对规则：窗口内「old 写 × new 读」按 backward；「new 写 × old 读」按 forward；同版本不判定。`blocked` 与 `unknown` 都会让计划停在该阶段。

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（场景、样例、演示步骤） |
| GET | `/workbench?sample=A|B|C` | 对比工作台（预填样例，含任务清单与滚动窗口预演） |
| POST | `/api/compare` | `{sample}` 或 `{old,new,consumers,tasks}`；返回 `run_id`/`summary`/`rollout`/`redirect`；非法输入 400 |
| GET | `/result/{run_id}` | 结果页（匹配表、类型结论、兼容级别、迁移、滚动切换窗口、broken_consumers） |
| POST | `/api/runs/{run_id}/rollout` | `{stage}` 推进/重试阶段；成功 200，失败/越序/未计划 409，非法参数 400 |
| GET | `/review` | 历史对比（含滚动阶段）+ SQL 下载 + 矩阵说明 |
| GET | `/api/runs`、`/api/runs/{run_id}` | 运行记录（含 `rollout` 阶段与执行状态） |
| GET | `/api/runs/{run_id}/migration.sql` | 下载迁移脚本（尾部附同一份滚动切换进度） |
| GET | `/api/matrix` | 完整类型矩阵 |
| GET | `/api/samples/{key}` | 样例原文（含 `tasks`） |
| GET | `/health` | 健康检查 |

## 目录

```text
app/            main.py + 7 个纯函数模块 + templates/ + static/
sample_data/    a/b/c 三组 old/new/consumers/tasks.json + index.json
tests/          test_matching / test_types / test_compat / test_migration / test_rollout / test_rollout_api / test_api
data/           运行记录（本地直跑时生成，已 gitignore）
```

## 已知限制

- 只做 schema 级别的静态判定，不读取 Parquet 文件字节，也不校验数据文件真实 id 编码；`consumers.json` 里的列引用来自人工/采集清单。
- 类型矩阵是显式小表，未覆盖的合法类型对（如 `string→double`、`timestamp→date`）返回 `unknown`，需人工确认，不自动推断。
- `ALTER TABLE` 为通用 SQL 片段（Spark/Trino 语义），不连接具体引擎执行；回填动作只给建议 SQL 与对象清单。
- 滚动窗口基于任务清单的静态时刻推算，不探测真实发布/下线事件；阶段推进是记录与核对，不会真的发布任务。
- 阶段执行状态按运行记录持久化，无并发多用户协调（进程内锁），单机演示定位。
- 运行记录保存最近 200 条，超出按修改时间裁剪；`data/` 被 gitignore，容器重建需通过命名卷保留。
- 无登录/权限，单机演示定位。
