# P2 验收与验证 Runbook（规模化）

> **为什么有这份文档**：和 P1 一样，开发环境里**无法执行任何命令**
> （shell 进程启动即失败，退出码 `0xC0000142`）。P2 的代码全部经过静态审查，
> 但**没有一行在真实解释器里跑过**。下面把"跑什么、每一步证明什么、失败了怎么分类"
> 一次写清楚，不依赖任何上下文。
>
> 按顺序执行。任一步失败先看第 6 节（失败特征对照表）。

---

## 1. P2 交付物与对应文件

| 交付项 | 主要文件 |
|---|---|
| 定时调度（cron/间隔 + 错峰抖动 + 执行窗口 + 任务关联） | `app/services/schedules.py`、`app/services/scheduler.py` |
| 保留策略与清理执行器（版本折叠/正文回收/会话日志清理） | `app/services/retention.py`、`app/services/blobs.py` |
| 通知渠道（邮件/Webhook/钉钉/企微/飞书 + 触发规则 + 发送记录） | `app/services/notifications.py`、`app/db/models.py`（`NotificationChannel`/`NotificationLog`） |
| 设备分组 | `app/services/groups.py`、`Device.group_id` |
| 运营接口 | `app/schemas/ops.py`、`app/api/v1/ops.py`、`app/api/deps.py`（`NotifierDep`/`SchedulerDep`） |
| 前端页面 | `web/src/views/{Groups,Schedules,Channels,Retention}View.vue` + `router`/菜单/`api/endpoints.js` |
| 迁移基线 | `migrations/versions/0001_initial_schema.py`、`tests/unit/test_migrations.py` |
| 1000 台压测 | `scripts/loadtest.py`、`tests/api/test_scale.py` |
| 新增测试 | `tests/api/test_ops.py`、`tests/api/test_group_scope.py`、`tests/api/test_scheduler.py`、`tests/api/test_scale.py`、`tests/unit/{test_schedules,test_retention,test_notifications,test_migrations}.py` |

---

## 2. 一次性准备（**必须先做**）

```powershell
cd F:\dszoon\backend
.venv\Scripts\activate
pip install -e ".[dev,web]"
```

> ⚠ **P2 新增了依赖 `apscheduler>=3.10,<4`**（在 `[web]` extra 里）。
> 不重装的话 `app.services.scheduler` / `schedules` 会在导入期抛
> `未安装 APScheduler，无法解析 cron 表达式`——而它被路由导入，表现为**整个 API 层
> 测试全部报错**（conftest 阶段就炸），看起来像"改坏了核心代码"。
>
> **用激活后的 `python`，不要用裸 `pytest`**（两者可能不是同一个解释器）。

### 2.1 ★ 如果你之前启动过应用：先处理旧库

仓库里现在有一个 **P1 时期建的** `backend/var/dszoon.db`（只含 P1 的 7 张表）。
`create_all` **只补建缺失的表，不会给已有表加列**，所以直接启动会缺
`devices.group_id` 与 `backup_tasks.schedule_id`。

P2 起启动时会**主动检查并直接报错**（而不是让你去面对"设备页整页 500、
`no such column: devices.group_id`"）：

```text
RuntimeError: 数据库 schema 比代码旧：缺少 devices.group_id、backup_tasks.schedule_id。
注意 create_all 只会补建缺失的表，**不会给已有表加列**。
开发/单机（SQLite）可以删掉数据库文件后重启，应用会按当前模型重建；
PostgreSQL 或需要保留数据的库请改用 Alembic 迁移。
```

> 报错里**不含** `database_url`：它会进日志、被贴进工单，而
> `postgresql+asyncpg://user:口令@host/db` 是带口令的。

**处理方式（二选一）**：

```powershell
# ① 开发环境：删掉旧库重启（里面的数据是 P1 冒烟测试留下的，没有保留价值）
Remove-Item var\dszoon.db
# （.secrets.json 要留着：删了它，已录入的设备密码就再也解不开了）

# ② 想保数据的库：从一开始就用 Alembic 管 schema（见第 5 节），
#    注意 P2 的基线迁移是"按当前模型建表"，对**已存在的旧表同样不会加列**，
#    所以 P1 老库没有自动升级路径——这也是把 auto_create_schema 与 Alembic
#    严格分开的原因。
```

> 这个检查本身是有价值的回归：`tests/unit/test_migrations.py` 里的
> `test_startup_detects_missing_column_in_existing_table` 会真的造一个"缺列的旧库"
> 来验证报错信息。

---

## 3. 逐步验证

### 步骤 0：10 秒体检（推荐先跑）

```powershell
cd F:\dszoon\backend
.venv\Scripts\activate
python scripts/preflight.py
```

只读、不建库不联网，一次性回答四个最容易卡住"第一次运行"的问题：

| 检查 | 不做的话会怎样 |
|---|---|
| 导入链（8 个关键模块） | 缺依赖或包级出口漏符号 → pytest 在**收集阶段**整体报错，看起来像"改坏了核心代码" |
| 依赖（含 P2 新增的 APScheduler） | 同上；APScheduler 缺了会让 `services/schedules` 导入期就炸 |
| `var/dszoon.db` 是不是旧 schema | 旧库 + 新代码 → 应用启动正常，设备页整页 500（`no such column: devices.group_id`） |
| `web/dist` 是否比 `web/src` 旧 | 界面停留在旧版本，新页面/新菜单看不到 |

退出码 0 表示没有阻塞项。它会直接给出该执行的命令（例如"删掉旧库"或"`npm run build`"），
且**不会**替你做任何修改。

### 步骤 1：全量回归

#### 1a. 先跑这 21 条（约 1 分钟，失败能立刻定位到子系统）

P2 的代码从未运行过，一次跑 270 条如果红了会很难分辨"是环境问题、导入链问题，还是某个
子系统写错了"。这一组每条压一个不同的子系统，**按"炸了以后影响面"排序**：

```powershell
python scripts/smoke.py
```

它就是 `python -m pytest -q <18 个 node id>` 的薄封装（名单在 `scripts/smoke.py` 里，
与下表一一对应；某条被改名时 pytest 会因"找不到用例"直接报错，名单必须跟着测试走）。

| 挂了的用例 | 说明哪里坏了 |
|---|---|
| `test_package_exports` 系列 | 包级出口漏符号 / 新模块导入就炸——**其它失败可能都是它的连锁反应，先修它** |
| `test_migrations` 系列 | Alembic 基线没建出与模型一致的表/索引，或启动期 schema 检查失效 |
| `test_startup` 系列 | 前端产物新旧提示（纯日志，不影响功能） |
| `test_next_run_daily_cron...` / `test_not_before_...` | cron 解析 / 时区换算 / "错过的班次不补跑" |
| `test_keeps_oldest_and_newest_always` | 版本折叠的保底规则（删错就是数据丢失） |
| `test_prune_unreferenced_...` | blob 回收的宽限期（防止删掉正在写库的版本） |
| `test_deliver_payload_shape` / `test_mail_starttls...` | 各机器人报文形状 / 邮件 SSL 与 STARTTLS 的映射 |
| `test_post_json_generic_webhook_does_not_interpret_code` | 通用 webhook 不解释业务错误码（否则成功会被记成失败） |
| `test_group_crud_and_membership` | 分组 CRUD 与成员关系落库 |
| `test_schedule_create_list_and_describe` | 定时策略接口 + cron 中文描述 + 时间带 Z |
| `test_manual_run_creates_task_without_moving_next_run` | 手工触发不改变既定班次 |
| `test_channel_webhook_url_is_redacted` | 机器人地址打码（凭据不外泄） |
| `test_failed_task_triggers_channel_and_writes_log` | **通知主链路**：任务失败 → 自动发 → 落记录 |
| `test_retention_run_prunes_and_reclaims_blobs` | 清理真的删版本并回收正文文件 |
| `test_batch_backup_by_group_only_touches_members` | 按组备份不越界（组外设备零采集） |
| `test_tick_fires_due_schedule_and_reports_final_status` | 调度器 tick → 任务 → 终态回写策略 |
| `test_scale_two_rounds_keep_runs_and_versions_distinct` | run ≠ snapshot + 内容寻址（200 台） |

#### 1b. 再跑全量

```powershell
python -m pytest -q
```

**期望**：全绿。P1 的 156 个用例必须仍然全绿（P2 不允许破坏 P1）。
P2 新增约 130 个测试（含参数化展开后约 150 个用例），合计应在 **280~340** 之间。
低于 280 说明有测试文件没被收集到；想知道确切总数可以：

```powershell
python -m pytest --collect-only -q | Select-Object -Last 3
```

**这一步证明**：P2 没有破坏 P1；新增服务与接口在真实解释器里可导入、可运行。

### 步骤 2：自检 8 步

```powershell
python scripts/selfcheck.py
```

**期望**：8 步全 OK（环境 → 语法 → 导入 → 驱动/清洗 → CLI/迁移配置 → 建库 →
端到端备份 → **P2 运营链路**）。第 3 步会导入**每一个** `app.*` 模块（包括新增的
`app.api.v1.ops`），`__all__` 里声明不存在的符号会在这里直接暴露；
第 8 步把 P2 的链路走一遍（分组/按组过滤设备/定时策略/预览/手工执行/渠道打码与测试/
保留策略与清理/审计动作），**发送环节是打桩的，不会真的往群里发消息**。

### 步骤 3：P2 聚焦测试（比全量快，定位更准）

```powershell
python -m pytest tests/unit/test_schedules.py tests/unit/test_retention.py `
  tests/unit/test_notifications.py tests/unit/test_migrations.py `
  tests/api/test_scheduler.py tests/api/test_ops.py tests/api/test_group_scope.py `
  tests/api/test_scale.py -q
```

**期望**：全绿。其中**这几条是最关键的**（它们各自守住一条设计不变量）：

| 测试 | 守住的不变量 |
|---|---|
| `test_missed_runs_are_not_caught_up` | 停机 3 天后不补跑，不会瞬间涌出几百个任务 |
| `test_manual_run_creates_task_without_moving_next_run` | 手工触发是插队，不改掉既定班次 |
| `test_tick_skips_when_previous_task_still_running` | 同一策略不叠任务 |
| `test_batch_backup_by_group_only_touches_members` | 按组备份**不越界**：组外设备一条记录都不该有 |
| `test_schedule_scoped_by_group_fires_members_and_records_link` | 按组策略只对组内设备发起，且任务能追溯到策略 |
| `test_task_list_can_filter_by_schedule_and_trigger` | 任务关联能**反向查**：按策略列出它发起过的任务 |
| `test_failed_task_triggers_channel_and_writes_log` | **通知主链路**：任务失败自动发通知并落发送记录 |
| `test_channel_event_subscription_is_respected` | 没订阅该事件的渠道不该被打扰 |
| `test_device_failed_streak_event_fires_only_after_repeated_failures` | "连续多次失败"是**真的会触发**的事件（不是只出现在正文里） |
| `test_schedule_with_impossible_window_is_rejected` | "永远不会有执行时刻"的策略在创建时就被 422 拒绝 |
| `test_audit_records_the_real_backup_scope` | 审计记的是**真实范围**（按分组发起的备份不会记成"范围为空"） |
| `test_scale_two_rounds_keep_runs_and_versions_distinct` | run ≠ snapshot：200 台跑两轮，版本数不翻倍 |
| `test_channel_webhook_url_is_redacted` / `test_channel_update_with_masked_url_keeps_real_url` | 机器人地址是凭据：出网打码、回传不写坏 |
| `test_manual_run_with_unmatched_scope_is_422` | 范围解析不出设备时报 422 而不是 500 |
| `test_retention_run_prunes_and_reclaims_blobs` | 清理要真的删版本**并回收正文文件** |
| `test_retention_runs_automatically_once_per_interval` | 保留策略会在后台**自动**跑，且同一间隔内不重复跑 |
| `test_prune_session_logs_only_removes_expired_files` | 会话日志只删过期的（按 mtime），没过期的绝不误删 |
| `test_mail_starttls_mode_does_not_use_ssl` | 邮件渠道的 SSL/STARTTLS 映射（587 走 STARTTLS，不能走 SSL） |
| `test_initial_migration_matches_models` | `alembic upgrade head` 建出的表与索引和模型完全一致 |
| `test_startup_detects_missing_column_in_existing_table` | 旧库缺列必须在**启动期**报错并给出修法 |

### 步骤 4：1000 台压测

```powershell
python scripts/loadtest.py --devices 1000 --latency 0.01 --concurrency 32 --rounds 2
```

**期望**：脚本最后打印 `✓ 通过`（零失败、单轮在 `--max-seconds` 内、正文文件数与
版本数符合预期），并给出：

```text
  devices: 1000
  round_seconds: [..., ...]
  runs: 2000            # 每设备每轮一条
  failed: 0
  snapshots: 1050       # 1000 + 变更的 50 台（未变更的设备不产生新版本）
  objects: 2            # 内容寻址：只有"原配置"和"变更配置"两份正文
  db_bytes / object_bytes / duration_p50_ms / duration_p95_ms
```

**这一步证明**：并发模型、串行落库、内容寻址、版本折叠在 1000 台量级下都成立。
`--report var/loadtest.json` 可以把结果留档进验收记录。

若有失败，脚本会额外打印**失败样本**（设备号 + 状态 + 错误码/原因），因为"失败了多少台"
没有可行动性——绝大多数情况下原因要么是夹具/厂商没对上（全是 `integrity_error`），
要么是并发把设备打崩了（`timeout`/`unreachable` 集中出现）。

> 说明：假设备只模拟 SSH 会话，**不模拟设备侧 CPU**。它测的是本系统的并发与落库能力；
> 真机上的耗时取决于设备响应速度与 VTY 数量。
> `--vendor h3c|ruijie` 会连带换用对应厂商的版本回显夹具（`VENDOR_FIXTURES`）：
> 型号是驱动按厂商正则抽的，夹具不匹配会表现为"全失败"。

### 步骤 5：迁移与"表的归属"

```powershell
# ① 全新库上用 Alembic 建表（生产路径）
$env:DSZOON_DATABASE_URL = "sqlite+aiosqlite:///./var/migrate-check.db"
$env:DSZOON_DATA_DIR = "./var/migrate-check"
$env:DSZOON_SECRET_KEY = "dummy"     # 避免生成 .secrets.json
alembic upgrade head
alembic current                      # 期望：0001_initial (head)
python -c "import sqlite3;c=sqlite3.connect('var/migrate-check.db');names=sorted(r[0] for r in c.execute('select name from sqlite_master'));print([n for n in names if not n.startswith('sqlite_')])"

# ② 生产模式缺表时必须报错（而不是偷偷建表）
$env:DSZOON_AUTO_CREATE_SCHEMA = "false"
$env:DSZOON_DATABASE_URL = "sqlite+aiosqlite:///./var/empty.db"
python -c "import asyncio;from app.core.config import get_settings;from app.db.session import Database;from app.main import prepare_database;asyncio.run(prepare_database(Database(get_settings().database_url), get_settings()))"
# 期望：RuntimeError，提示先执行 alembic upgrade head

# 清理
Remove-Item Env:DSZOON_AUTO_CREATE_SCHEMA, Env:DSZOON_DATABASE_URL, Env:DSZOON_DATA_DIR, Env:DSZOON_SECRET_KEY
Remove-Item -Recurse -Force var/migrate-check*, var/empty.db
```

**期望**：① 里能看到 12 张业务表 + `alembic_version`；② 报错信息里带确切命令。

### 步骤 6：前端构建 + 页面走查

```powershell
cd F:\dszoon\backend\web
npm install          # 只有依赖有变化才需要
npm run build        # 产出 web/dist（后端会挂载它）
```

然后 `uvicorn app.main:create_app --factory --port 8000`（在 `backend/` 下），打开
`http://127.0.0.1:8000`：

| 页面 | 走查点 |
|---|---|
| **设备分组** | 新建分组 → 打开"成员" → 勾选设备 → 保存（整体替换）→ 设备数正确；删除分组后**设备仍在**（只是分组变空） |
| **定时策略** | 新建 cron 策略（如 `0 2 * * *`）→ 列表"计划"列显示"每天 02:00"→ 点"预览"看接下来 5 次时刻 → 点"立即执行"→ 到"备份任务"页能看到任务，且策略的"下次执行"**没有被改动**；再点"任务历史"能看到这次任务（发起列显示"手工"，定时发起的显示"定时"） |
| **通知渠道** | 新建钉钉/企微渠道（整条机器人地址粘贴）→ 列表里地址已打码（`access_token=***`）→ 点"测试"→ 群里收到消息；再点"发送记录"能看到这条 |
| **保留策略** | 改"未变更版本按天折叠"为 0 并保存 → 点"立即清理一次"→ 弹出的成功提示里带删除/回收数量，页面"上次清理"处同步更新。**另外**：不点任何按钮，启动应用后等一个轮询周期（默认 30s），"上次清理"也会自动更新——`DSZOON_RETENTION_INTERVAL_HOURS` 默认 24 小时，首次启动（`last_run_at` 为空）会先跑一次 |

> 前端页面**必须重新 `npm run build`**：P2 新增了 4 个页面与路由，`web/dist` 里的旧
> 产物不会自动包含它们（只重启后端不生效）。

---

## 4. P2 的设计取舍（值得复核的 6 处）

复核时若认为某处取舍不合适，改动都很局部：

1. **调度器轮询数据库**（默认 30s），不注册 APScheduler job。
   换来"数据库是唯一事实来源、重启自动恢复"；代价是精度受轮询间隔限制——对"凌晨备份"
   这种粒度完全够用，远小于错峰抖动的分钟级量级。
2. **错过的班次不补跑**（`not_before=now`）。停机 3 天后重启不该瞬间涌出几百个任务。
3. **手工触发不推进 `next_run_at`**。插队不该改变"每天 02:00"这件事。
4. **保留策略默认永久保留变更版本**。磁盘 vs 可追溯性的取舍，默认选可追溯。
5. **机器人 URL 打码 + 回传打码值时保留真实地址**。牺牲"在界面上复制完整地址"，
   换"列表接口泄露不了群机器人凭据"。
6. **生产缺表就报错**（`DSZOON_AUTO_CREATE_SCHEMA=false`），不偷偷 `create_all`。
7. **自动清理正文时留 1 小时宽限期**（`retention.BLOB_GRACE_SECONDS`）。采集是"先写正文
   文件、再插快照行"，任务进行中触发清理会把"文件已写、行未提交"的那份当成无主垃圾。
   代价是：刚删完版本时"回收 0 个文件"是正常的，下次清理才会收掉。

---

## 5. 仍未验证 / 需要硬件或他人操作

| 项 | 状态 | 说明 |
|---|---|---|
| P2 全部后端与前端代码 | **未执行**（本环境 shell 不可用，仅静态审查） | 以步骤 1~3 为准 |
| 1000 台真实设备 | **未做** | `scripts/loadtest.py` 用内存假设备验证系统侧能力；真机需另做现场压测 |
| 华三 / 锐捷真机 | **未做** | 见 `docs/SPIKE_REPORT.md` |
| 邮件渠道真实发信 | **未做** | 需要可用的 SMTP 账号；`test_channel` 单测用打了桩的发送函数 |
| 钉钉/企微/飞书真实群 | **未做** | 需要在群里建机器人，粘贴地址后点"测试" |

---

## 6. 失败特征对照表（P2 新增）

| 现象 | 原因 / 处理 |
|---|---|
| 收集测试时报 `未安装 APScheduler` 或 `ImportError: apscheduler` | 没重装依赖：`pip install -e ".[dev,web]"` |
| 全部 API 测试报 `ImportError: cannot import name ... from 'app.api.deps'` | 包级出口漏符号；`tests/unit/test_package_exports.py` 会精确定位 |
| `422` 且 `extra` 里说 `kind: Input should be 'smtp'...` | 渠道类型名是 `smtp`（不是 `email`）；`test_channel_kind_literal_matches_service_enum` 守住这条 |
| 定时策略到点不执行 | ① 是否启用；② `next_run_at` 是否为空；③ 调度器是否被关；④ 上一轮是否还在跑（`skipped_busy`）；⑤ 执行窗口是否把时刻过滤掉了（用"预览"验证） |
| 策略建好了但"下次执行"一直是 `-` | 现在建不出来（cron 与窗口无交集会直接 422）。若真出现，说明库里那行被直接改过，看启动/调度日志里的"算不出下一次执行时刻"警告 |
| 策略"上次结果"永远停在"排队中" | `BackupRunner._sync_schedule` 没跑到（任务终态没回写）；`test_tick_fires_due_schedule_and_reports_final_status` 覆盖 |
| 压测报"正文文件数偏多" | 内容寻址失效（同一内容写了多个文件），或 `--change-ratio` 变更内容各不相同 |
| 压测报"版本数超过预期" | run 与 snapshot 混淆了：不变的内容也在建新版本 |
| `alembic upgrade head` 无输出且没建表 | 版本目录里没有基线迁移；`tests/unit/test_migrations.py` 会直接报出来 |
| `alembic` 报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0xa5` | **`alembic.ini` 里出现了非 ASCII 字符**（最常见是中文注释）。alembic 用 locale 编码读这个文件，中文 Windows 上是 GBK → 直接读不出来。注释必须是英文（有测试守着） |
| 改了模型但 `alembic upgrade head` 没变化 | 基线迁移不会自动跟上模型变化——新增/修改表**必须** `alembic revision --autogenerate` |
| 启动报"数据库里没有 users 表" | 生产模式（`DSZOON_AUTO_CREATE_SCHEMA=false`）下没有先跑 `alembic upgrade head` |
| 启动报"数据库 schema 比代码旧：缺少 devices.group_id…" | 沿用 P1 老库又没跑迁移。见 2.1 节：开发环境删掉 `var/dszoon.db` 重启（**保留 `.secrets.json`**） |
| 前端新页面打不开（404/空白） | 没重新 `npm run build`；`web/dist` 是旧产物 |
