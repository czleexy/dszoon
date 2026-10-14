# dszoon 网络设备配置备份系统

> **当前进度**：P0（采集层）已完成并真机验证；**P1（Web 后端/前端/部署文件）、  
> P2（规模化：调度、分组、通知、保留）、P3（合规基线、审计、指标）均已交付**；  
> P4（配置下发与回滚、SNMP 自动发现、Git 存储后端、Celery）进行中。  
> 各阶段范围、里程碑与安全约束见 `../DEVELOPMENT_PLAN.md`。
>
> **怎么确认这套部署是好的**：`python scripts/preflight.py`（10 秒只读体检）→  
> `python scripts/selfcheck.py`（分层自检）→ `python scripts/smoke.py`（冒烟子集）→  
> `python -m pytest -q`（完整回归）。验收步骤见 `../docs/P1_RUNBOOK.md`、  
> `../docs/P2_ACCEPTANCE.md`、`../docs/P3_ACCEPTANCE.md`。
>
> **部署文档见 [`../docs/DEPLOYMENT.md`](../docs/DEPLOYMENT.md)**：Docker / systemd /  
> 离线内网包 / Windows 四种形态，含备份恢复、升级与故障处置。
>
> 配置下发/回滚仍是**只读**设计：接口已预留（`VendorDriver.deploy`），  
> v1.0 一律抛 `DeployNotSupported`。

## 目录

```
app/
  main.py            FastAPI 应用工厂 + 生命周期（建表、初始管理员、优雅关闭）
  errors.py          应用异常（服务层不感知 HTTP，映射在 main 里统一做）
  core/
    config.py        配置（DSZOON_* 环境变量）+ 密钥自动生成与落盘
    crypto.py        设备凭据加解密（Fernet）
    security.py      口令哈希（scrypt）+ JWT + 角色
  db/
    base.py          声明基类 + "统一存朴素 UTC 时间"的约定
    models.py        users / credential_profiles / devices / backup_tasks
                     / backup_runs / config_snapshots / audit_logs
    session.py       引擎与会话（SQLite 自动打开外键约束）
  schemas/           对外 DTO（凭据字段永不出现在响应里）
  services/
    blobs.py         配置正文内容寻址存储（原文归档 + 去重）
    devices.py       设备/凭据 CRUD、批量导入、构造采集目标
    backups.py       批量任务编排（并发采集 + 串行落库 + SSE 事件总线）
    snapshots.py     版本读取与脱敏
    diffs.py         版本对比 + "重要变更"识别
    audit.py         审计写入
  api/               deps（鉴权/角色）+ v1 路由（auth/meta/devices/backups/snapshots）
  collector/         ★ P0 采集层（会话、清洗、完整性校验、引擎、驱动、归档）
tests/
  fixtures/          真实回显样本（脱敏）——项目最重要的测试资产
  mock_device/       假设备 SSH 服务器（YAML 剧本回放，测采集层）
  support/           内存假设备会话（测 API 层，不碰网络）+ 测试环境构建
  unit/ integration/ 采集层测试
  api/               接口测试
web/                 ★ Vue3 + Element Plus 前端（构建到 web/dist，由后端挂载）
deploy/              部署：Dockerfile / docker-compose / nginx / systemd / 离线包脚本
alembic.ini          Alembic 配置（连接串从 DSZOON_DATABASE_URL 读）
migrations/          数据库迁移（生产用；开发/测试用 create_all）
scripts/
  spike_probe.py     P0 真机实测探针
  selfcheck.py       一键自检（环境/语法/导入/建库/端到端），排查问题先用它
```

## 快速开始

```bash
cd backend
python -m venv .venv && .venv\Scripts\activate      # Windows
# source .venv/bin/activate                          # Linux
pip install -e ".[dev,web]"                          # web = 后端 Web 依赖（含 apscheduler，P2 新增）

# ⓿ 首跑前的 10 秒体检（只读）：导入链 / 依赖 / 旧库 / 前端产物是否过期
python scripts/preflight.py

# ① 先跑自检（8 步逐级验证：环境 → 语法 → 导入 → 驱动/清洗 → CLI/迁移配置 → 建库 → 端到端备份 → P2 运营链路）
#    比 pytest 定位快：一次报 20 个失败时，它能立刻指出是依赖、导入、建库还是逻辑问题
python scripts/selfcheck.py
# ② P2 冒烟子集（21 项，约 1 分钟）：每条压一个子系统，失败能立刻定位范围
python scripts/smoke.py
# ③ 完整回归测试（★ 用 python -m pytest，别用裸 pytest：两者可能不是同一个解释器）
python -m pytest -q

# ④ 启动后端 API（注意 --factory）
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
# 首次启动会建库并创建管理员 admin；口令在启动日志里打印一次
# （或用 DSZOON_ADMIN_PASSWORD 指定）
# 接口文档：http://127.0.0.1:8000/docs
```

### 前端（Vue3 + Element Plus）

```bash
cd backend/web
npm install
npm run dev        # http://127.0.0.1:5173（已配好 /api 代理到 8000）
npm run build      # 产物落到 web/dist
```

构建后再启动后端，**同一个端口就能同时提供前后端**（`app/main.py` 会自动挂载  
`web/dist`），访问 <http://127.0.0.1:8000> 即可。

> 前端用 **hash 路由**（`#/devices`）是有意的：FastAPI 的 StaticFiles 不会为  
> `/devices/1` 这类深链接做 SPA 回退（会 404），hash 路由下刷新和分享链接都不会 404。

页面：登录 · 看板（统计 + 一键全备）· 设备（CRUD / CSV 导入 / 连接测试 / 单台与批量备份）  
· 设备详情（历史版本 / 配置查看 / 脱敏切换 / 版本 diff / 采集记录）  
· 备份任务（列表 + **SSE 实时进度与日志**）· 凭据 · 用户（仅管理员）。

前端通过 `fetch + ReadableStream` 手工解析 SSE，而不是原生 `EventSource`——  
后者不支持自定义请求头，而我们的接口需要 `Authorization: Bearer`；  
用 query 传令牌会把令牌写进访问日志，所以没那么做。

```bash
# P0：对真机做实测，产出 report.md + 夹具
python scripts/spike_probe.py --vendor huawei --host 10.10.10.1 \
    --username admin --out .spike/HW-CORE-01
```

### 命令行工具（不开界面也能用）

```bash
python -m app.cli init-db               # 建表 + 初始管理员（开发用；输出随机口令）
python -m app.cli gen-key               # 生成设备凭据加密密钥
python -m app.cli stats                 # 台账与存储概览
python -m app.cli create-user ops --role operator
python -m app.cli reset-password admin  # 忘记管理员口令时的救命命令
python -m app.cli list-devices --vendor huawei
python -m app.cli backup --all          # 跑一次全量备份（cron / 运维脚本可用）
python -m app.cli backup --device-id 3 --precheck --retries 2
```

`backup` 用的是与 Web 完全相同的编排逻辑（含落库、版本判定、失败留痕），  
所以它的结果在界面上同样可见。装好包后也可以直接用 `dszoon` 命令。

### 数据库迁移（生产用 Alembic）

```bash
cd backend
alembic upgrade head                        # 全新库：应用基线迁移（0001_initial_schema）
alembic revision --autogenerate -m "xxx"    # 模型变了：生成增量迁移
alembic current / alembic history           # 查看状态
alembic downgrade -1                        # 回滚一步
```

连接串从 `DSZOON_DATABASE_URL` 读（**不写在 `alembic.ini` 里**，避免"迁移改的是 A 库、  
应用连的是 B 库"）。

> ⚠ **`alembic.ini` 必须保持纯 ASCII**（注释也不要写中文）：alembic 读它用的是  
> **locale 编码**（`read_config_parser` 里写死 `encoding="locale"`），中文 Windows 上是  
> GBK，于是 `alembic upgrade head / revision / current` 会直接抛  
> `UnicodeDecodeError: 'gbk' codec can't decode byte 0xa5`——而报错信息里完全看不出是注释  
> 惹的祸（第一次真实运行就踩到了）。`logging.fileConfig` 读同一个文件、同样是 locale  
> 编码，所以会连日志配置一起带崩。`tests/unit/test_migrations.py::test_alembic_ini_is_ascii_only`  
> 盯着这一点。

**P2 起表的归属是明确的**：要么应用启动时自动建（`DSZOON_AUTO_CREATE_SCHEMA=true`，  
默认，开发/单机图省事），要么 Alembic 建（生产置 `false`）。生产环境下若库里没有  
`users` 表，启动会**直接报错**并提示执行 `alembic upgrade head`，而不是偷偷  
`create_all` 把表补上——偷偷补表会让迁移历史与真实 schema 分叉，某天 alembic 报  
"表已存在/列不存在"时几乎查不出是哪一步跑偏。

- 基线迁移 `migrations/versions/0001_initial_schema.py` 直接用模型的 metadata 建表，  
  因此**不可能与模型漂移**；`tests/unit/test_migrations.py` 会在临时库上真正跑一次  
  `upgrade head`，再逐表比对表名与索引名。
- ★ 一旦有实例升级过基线，后续 schema 变化**必须**用 `--autogenerate` 生成新的迁移文件，  
  不要再改基线（改了对已经升级过的库无效）。
- ⚠ autogenerate 是"对比模型与库"：目标库已经有表时它会认为"没差异"，产出**空迁移**。  
  正确顺序：新库 → `autogenerate` → 人工过一眼 → `upgrade head` → 再启动服务。
- ⚠ autogenerate 不认识表/列**重命名**（会当成删除+新建，数据就没了），也不生成数据迁移。

### 常用配置（环境变量）

| 变量                                | 默认                                    | 说明                                                                     |
| --------------------------------- | ------------------------------------- | ---------------------------------------------------------------------- |
| `DSZOON_DATABASE_URL`             | `sqlite+aiosqlite:///./var/dszoon.db` | 生产用 `postgresql+asyncpg://user:pass@host:5432/db`                      |
| `DSZOON_DATA_DIR`                 | `var`                                 | 配置正文与会话日志的根目录                                                          |
| `DSZOON_SECRET_KEY`               | 自动生成到 `var/.secrets.json`             | **设备凭据加密密钥**；换掉它 = 所有设备密码要重录                                           |
| `DSZOON_JWT_SECRET`               | 自动生成到 `var/.secrets.json`             | JWT 签名密钥                                                               |
| `DSZOON_ADMIN_PASSWORD`           | 随机（启动日志打印一次）                          | 初始管理员口令，仅首次建库时生效                                                       |
| `DSZOON_BACKUP_CONCURRENCY`       | `20`                                  | 全局并发上限（设备 VTY 有限，别调太大）                                                 |
| `DSZOON_AUTO_CREATE_SCHEMA`       | `true`                                | 启动时是否自动建表；**生产用 Alembic 时置 `false`**                                   |
| `DSZOON_SCHEDULER_ENABLED`        | `true`                                | 是否启用定时调度器（多实例部署时只让一个实例开）                                               |
| `DSZOON_SCHEDULER_POLL_SECONDS`   | `30`                                  | 调度轮询间隔；精度受它限制，对"每天凌晨备份"足够                                              |
| `DSZOON_RETENTION_INTERVAL_HOURS` | `24`                                  | 自动执行保留策略的间隔（0 = 只允许手工清理）。⚠ 它挂在调度器循环里，`SCHEDULER_ENABLED=false` 时不会自动清理 |
| `DSZOON_COMPLIANCE_ENABLED`       | `true`                                | 采集到新版本后是否自动跑合规基线                                                       |
| `DSZOON_COMPLIANCE_RULES`         | 空（用内置基线）                              | 自定义规则文件路径，JSON 数组；给了就**替换**内置基线                                        |
| `DSZOON_CORS_ORIGINS`             | `["http://localhost:5173"]`           | 前端开发服务器地址                                                              |

> ⚠ cron 里的「每天 02:00」按**进程本地时区**判定（容器里由 `TZ` 决定，见  
> `deploy/.env.example`）。`TZ` 不对会让任务在半夜跑，而策略描述上仍写着"每天 02:00"——  
> 这类问题只能靠时区设置本身避免。

> 生产部署请把两个密钥改用环境变量，并备份 `.secrets.json`：**丢失 secret_key 等于  
> 所有设备密码要重录**。

## API 一览（P1）

| 方法                          | 路径                                                           | 说明                                                                  |
| --------------------------- | ------------------------------------------------------------ | ------------------------------------------------------------------- |
| POST                        | `/api/v1/auth/login`                                         | 登录，返回 Bearer 令牌                                                     |
| GET / POST                  | `/api/v1/auth/me` · `/auth/change-password`                  | 当前用户 / 改口令                                                          |
| GET / POST / PATCH          | `/api/v1/users`                                              | 用户管理（仅 admin）                                                       |
| GET                         | `/api/v1/meta/vendors` · `/meta/stats`                       | 驱动列表 / 看板统计                                                         |
| GET / POST / PATCH / DELETE | `/api/v1/devices`                                            | 设备 CRUD（支持 q/vendor/site/enabled/**group_id** 过滤）                   |
| POST                        | `/api/v1/devices/import`                                     | 批量导入（前端解析 CSV 后 POST JSON 行，逐行报错）                                   |
| POST                        | `/api/v1/devices/{id}/test`                                  | 连接测试（成功时顺手补全型号/版本/设备名）                                              |
| POST                        | `/api/v1/devices/{id}/backup`                                | 立即备份单台设备                                                            |
| GET / POST / PATCH / DELETE | `/api/v1/credentials`                                        | 凭据管理（密码只写不读）                                                        |
| POST                        | `/api/v1/backups`                                            | 发起批量备份（device_ids / group_id / site / vendor / all_enabled）         |
| GET                         | `/api/v1/backups` · `/backups/{id}` · `/backups/{id}/stream` | 任务列表（可按 `status` / `trigger` / `schedule_id` 过滤）/ 详情 / **SSE 实时进度** |
| POST                        | `/api/v1/backups/{id}/cancel`                                | 取消任务                                                                |
| GET                         | `/api/v1/runs`                                               | 采集记录（失败也留痕）                                                         |
| GET                         | `/api/v1/devices/{id}/snapshots` · `/snapshots/{id}/content` | 版本列表 / 正文（`format=raw\|redacted`）                                   |
| GET                         | `/api/v1/devices/{id}/diff`                                  | 版本对比（先归一化，再给 diff + 重要变更）                                           |
| GET                         | `/healthz` · `/readyz`                                       | 存活 / 就绪                                                             |


## API 一览（P2 新增）

| 方法                                | 路径                               | 说明                                  |
| --------------------------------- | -------------------------------- | ----------------------------------- |
| GET / POST / PATCH / DELETE       | `/api/v1/groups`                 | 设备分组（删分组**不删设备**，只解除归属）             |
| PUT                               | `/api/v1/groups/{id}/devices`    | 调整成员（`replace=false` 为追加）           |
| GET / POST / GET / PATCH / DELETE | `/api/v1/schedules`              | 定时策略（cron / 间隔 + 错峰抖动 + 执行窗口 + 范围）  |
| GET                               | `/api/v1/schedules/{id}/preview` | 接下来 N 次执行时刻（带抖动的策略是"大概范围"）          |
| POST                              | `/api/v1/schedules/{id}/run`     | 手工立即执行（**不推进下一次计划时间**，插队不改节奏）       |
| GET / POST / PATCH / DELETE       | `/api/v1/channels`               | 通知渠道（smtp / webhook / 钉钉 / 企微 / 飞书） |
| POST                              | `/api/v1/channels/{id}/test`     | 发一条测试消息（不通时返回 `ok=false` 而不是 500）   |
| GET                               | `/api/v1/notification-events`    | 可用事件与渠道类型（前端渲染复选框用，避免两边枚举对不上）       |
| GET                               | `/api/v1/notification-logs`      | 发送记录（按渠道/结果过滤；渠道删了记录仍可读）            |
| GET / PUT                         | `/api/v1/retention`              | 保留策略（单例）                            |
| POST                              | `/api/v1/retention/run`          | 立即执行清理，返回删除摘要（仅 admin）              |

写权限：**改配置要 operator，删分组/删策略/删渠道/改保留策略/执行清理要 admin**；  
所有写操作都写审计日志。

### 通用 Webhook 的报文约定（自研接收端按这个实现）

```json
{
  "event": "task_failed,significant_change",
  "subject": "[dszoon] 备份任务 #12 部分失败",
  "text": "任务：#12 夜间巡检\n触发：定时\n……",
  "task_id": 12,
  "source": "dszoon",
  "token": "（渠道里配了校验令牌时才有）"
}
```

- 只要 HTTP 200 即视为投递成功——**不解释响应体里的 `code`/`errcode`**：那是钉钉/企微/  
  飞书的约定，套到自研接收端上会把"已经投递成功"记成失败（见设计要点第 25 条）。  
  想表达失败就返回非 2xx 状态码。
- 回复体不需要内容；若要回传信息，请用 2xx 状态码，别用 `code != 0`。

**审计要记"真实范围"**：按分组发起的批量备份、以及定时策略的范围（`scope`）都必须进  
审计详情。只记 `device_ids/site/vendor/all_enabled` 的话，一条按分组发起的备份在审计里  
就是"范围为空"——事后查"昨晚那次全量是谁发起的、覆盖了哪些设备"时会一无所知。

角色是**包含式**的：`admin > operator > auditor > viewer`。  
查看配置**明文**（`format=raw`）需要 auditor 及以上，且每次都会写审计。

## API 一览（P3 新增）

| 方法   | 路径                                        | 权限          | 说明                          |
| ---- | ----------------------------------------- | ----------- | --------------------------- |
| GET  | `/api/v1/compliance/rules`                | 登录          | 当前生效的规则集 + 指纹（确认"跑的是哪一版规则"） |
| GET  | `/api/v1/compliance/summary`              | 登录          | 全场合规概况（看板用）                 |
| GET  | `/api/v1/compliance/report`               | 登录          | "设备 × 规则"矩阵 + 合规率（按厂商/站点分组） |
| GET  | `/api/v1/compliance/export?fmt=csv\|json` | 登录          | 导出报表（**写审计**：不合规清单本身就是攻击面）  |
| POST | `/api/v1/compliance/run`                  | operator    | 立即检查（不重新采集，只评估各设备最新版本）      |
| GET  | `/api/v1/devices/{id}/compliance`         | 登录          | 单台设备的最新结果 + 历史              |
| POST | `/api/v1/compliance/cache/reset`          | operator    | 改完规则文件后清缓存（不必重启进程）          |
| GET  | `/api/v1/audit-logs`                      | **auditor** | 审计日志查询（动作/用户/对象/时间范围/模糊搜索）  |
| GET  | `/api/v1/audit-logs/actions`              | **auditor** | 动作字典（前端筛选下拉用）               |
| GET  | `/api/v1/audit-logs/downloads`            | **auditor** | **等保那一问**：谁在何时下载了哪台设备的配置    |
| GET  | `/api/v1/audit-logs/export?fmt=csv\|json` | **auditor** | 导出审计日志（**本身也写审计**）          |

> 审计相关接口要 **auditor 起步**：日志里写着谁在什么时候看过哪台设备的明文配置，  
> 它比配置本身更该受限。导出也留痕——否则"审计能力"可以被用来规避审计。  
> CSV 一律带 UTF-8 BOM（不然 Excel 双击打开是乱码）。

### Prometheus 指标（P3）

`GET /api/v1/metrics` —— exposition 文本格式，**不要求登录**（Prometheus 抓取端不带会话，  
与 `/healthz`、`/readyz` 同类）。

- 指标口径与取舍写在 `app/services/metrics.py` 的模块说明里；两条关键取舍：  
  **不造假直方图**（表里只有原始记录，硬造分位数会让面板显示错误的 P95）、  
  **标签基数受控**（设备名/IP 绝不进标签，否则 1000 台设备会打爆 Prometheus）。
- 重点看 `dszoon_schedules_due_total`：**持续 >0 说明调度器没在跑**（到期策略积压），  
  这是单机部署里最值得配告警的一条。
- ⚠ "不要求登录"不等于"可以暴露到公网"：指标里有设备总数、失败次数、不合规数量。  
  生产建议在 nginx 上限制 `/api/v1/metrics` 的来源 IP。

### 合规基线（P3）

规则是**数据**（YAML），不是代码：运维不改 Python 就能调整基线。

```yaml
- id: no-telnet-server
  vendors: [huawei, h3c, ruijie]       # 不给 = 所有厂商
  desc: 禁止开启 Telnet 服务
  severity: high                       # high → 违规即 fail 并发通知；medium/low 只提示
  assert_absent: '^\s*telnet\s+(?:server\s+)?enable\s*$'
  message: 关闭 telnet，运维统一走 SSH
```

- 内置基线在 `app/compliance/baselines/baseline.yaml`（16 条，含三家厂商语法差异）；  
  用 `DSZOON_COMPLIANCE_RULES` 指向自己的 YAML 即可**整体替换**（不是叠加）。
- **采集成功后自动评估**（产生新版本时；内容没变的版本不重复评估）。关掉用  
  `DSZOON_COMPLIANCE_ENABLED=false`。
- 评估用**归档原文**（否则 `community public` 这类规则在脱敏后必然失效），但  
  **违规行摘录入库前脱敏**——结果表不能变成口令的第二份明文副本。
- 正则写错会在**加载时**报错，不会静默跳过：一条"看着生效、其实没检查"的规则比没有更危险。
- 报表里**没评估过的设备也会出现**（判定 `unknown`）；运维要看的恰恰是"谁还没查过"。

## 最小可用示例（直接用采集层，不经 Web）

```python
import asyncio
from app.collector import DeviceTarget, backup_many
from app.collector.storage import ArtifactStore

async def main():
    targets = [
        DeviceTarget(host="10.10.10.1", vendor="huawei",
                     username="admin", password="***", hostname="HW-CORE-01"),
        DeviceTarget(host="10.20.10.1", vendor="h3c", username="admin", password="***"),
    ]
    outcomes = await backup_many(
        targets, store=ArtifactStore("var/archive"), concurrency=20, precheck=True, retries=2
    )
    for outcome in outcomes:
        print(outcome.target.display, outcome.summary())

asyncio.run(main())
```

## 设计要点（改代码前请先读）

1. **提示符必须先泛化后收紧**：登录时用泛化正则，拿到真实设备名后立刻换成  
   字面量正则（`driver.prompt_re(hostname)`）。否则配置正文里一行 `[XXX]`  
   就会让读取循环"以为命令跑完了"，采到一半的内容。见  
   `test_strict_prompt_rejects_foreign_hostname`。
   - 反查设备名（`match_prompt`）**只认无歧义的提示符形态**。华为/H3C 的  
     `[H3C-CORE-02-Vlan-interface10]` 里设备名与视图名用同一个连字符分隔，  
     正则没有依据判断切点，因此返回 `None`，由台账里的设备名兜底——猜错比不猜危险得多。
   - `return<HUAWEI>` 这类"提示符与正文粘连"的形态，**只在严格提示符下**才处理  
     （`session.strict_prompt`）。泛化正则下 `description [UPLINK-TO-DC-SPINE]`  
     的结尾形态与提示符无法区分，开了就会把正文行截断。
2. **分页符只在结尾才算分页**：`has_more_token` 必须锚定缓冲区结尾。  
   用"全文搜索"会让历史页的分页符导致对已结束的输出狂发空格。见  
   `test_has_more_token_only_matches_at_end`。
3. **预处理命令失败不致命，但必须报警**：设备拒绝 `screen-width` 时配置仍有  
   折行风险，警告会进 `outcome.warnings` 与 run 记录，绝不静默成功。
4. **内容残了就不算成功**：`integrity_error` 状态的内容**仍然归档**（用于排障），  
   但不计入成功，也不参与"配置变更"判断。
5. **同设备串行**：`backup_many` 按 `host:port` 加锁，一台设备任何时刻只有一个会话。
6. **密码不进任何文本**：`send_command(secret=True)` 的入参不写日志、不参与清洗；  
   提权警告用 `******` 代替命令文本。API 层同理：凭据 schema 里没有 password 字段。
7. **交互确认自动回答**：设备会对某些命令弹 `[Y/N]`（真机实测华为 VRP5 的  
   `screen-width 512` 就是），不回答就会一直干等到超时，报错还只是含糊的  
   "会话空闲 N 秒无数据"。机制在 `session._match_confirmation`，规则由驱动  
   YAML 的 `confirm_prompts` 声明。**配置下发（deploy）不得复用这个机制自动放行。**
8. **"只读"要可验证**：`integrity.check_setup_leakage()` 会在采集后复查  
   "会话预处理命令有没有被写进配置"，命中即报警——采集只允许改会话状态，  
   不允许改设备配置。
9. **run 与 snapshot 分开**：同一份配置采 100 次只有 1 个 snapshot，但会有 100 条  
   run。这样"无变更"的巡检不会把版本库撑爆，而每一次失败/成功都可追溯。
10. **时间统一存朴素 UTC**，出 API 时补 `Z`（`app.schemas.common.UtcDatetime`）。  
    SQLite 不保存时区，混用 aware/naive 会造成"SQLite 上对、PostgreSQL 上错"。
11. **并发采集、串行落库**：采集是网络 I/O 所以并发；写库用一把 asyncio.Lock  
    串行化（SQLite 并发写会 database is locked，PostgreSQL 也需要行锁）。
12. **任务事件总线是进程内的**：单机够用且简单；横向扩展时换 Redis pub/sub +  
    Celery，接口不用动。重启会丢队列中的任务（P2 未解决，见"已知限制"）。
13. **调度器轮询数据库，而不是注册内存 job**（P2）：策略是数据库里的数据，随时被  
    增删改启停；注册 job 就必须维护"数据库行 ↔ 内存 job"的双向同步，少同步一处就  
    出现"改了策略不生效""删了策略还在跑"。轮询方案的唯一事实来源是数据库，重启自动  
    恢复。APScheduler 仍然在用——只用它的 `CronTrigger` 解析 cron 与算下次时间。
14. **错过的班次不补跑**（P2）：`compute_next_run(..., not_before=now)`。停机 3 天后  
    重启，不能瞬间涌出几百个任务把设备全打死。同时"下一次"从**计划时刻**往后推而不是  
    从"当前时间"推，否则每次 tick 的延迟会累积，02:00 的班次会越跑越晚。
15. **手工触发不推进计划时间**（P2）：`POST /schedules/{id}/run` 是插队，不能把  
    "每天 02:00"改成"手工执行后 24 小时"。
16. **同一个策略不叠任务**（P2）：上一轮还在跑就跳过本次（`skipped_busy`）。  
    1000 台一次要跑十几分钟，而策略可能是每 5 分钟一次。
17. **通知失败绝不影响备份**（P2）：`Notifier.dispatch_task_result()` 吞掉所有异常，  
    失败写 `NotificationLog` 与渠道的 `last_error`。渠道配置不通时界面能直接看到原因。
18. **机器人 webhook 地址本身就是凭据**（P2）：钉钉 `access_token`、企微/飞书的 `key`  
    就在 URL 里，拿到它谁都能往公司群里发消息。所以列表/详情接口一律打码  
    （`?access_token=***`），回传打码值时保留库里的真实地址。
19. **保留策略默认保守、但会自己跑**（P2）：最早 1 个 + 最新 N 个版本**永久保留**，  
    变更版本默认永久保留，未变更版本按"每天 1 个"折叠 N 天。宁可多占磁盘，也不能  
    出现"配置被清理了、出事时没有基线可对比"。同时它**每天自动执行一次**  
    （`DSZOON_RETENTION_INTERVAL_HOURS`，靠策略行的 `last_run_at` 保证幂等）——  
    只提供"立即清理"按钮的策略在真实环境里等于永远不跑。
20. **清理是全局回收，且留宽限期**（P2）：正文按内容寻址、跨设备共享，所以删完快照行后  
    要按"仍被引用的 key 集合"回收磁盘，且**只删 `<64位hex>.cfg` 形态的文件**——宁可漏回收  
    也不能误删以后可能新增的导出件。回收还会跳过"1 小时内刚写下的文件"：采集是"先写正文、  
    再插快照行"，任务进行中触发清理会把还没写库的版本正文当成无主垃圾删掉  
    （`retention.BLOB_GRACE_SECONDS`）。所以"删了 N 个版本却回收 0 个文件"是可能的，  
    下次清理会收掉。
21. **表的归属只有一个**（P2）：应用自动建（开发）或 Alembic 建（生产），二者不混用。  
    生产环境下缺表直接报错并提示 `alembic upgrade head`。
22. **启动时检查 schema 漂移**（P2）：`create_all` 只补建缺失的表，**不会给已有表加列**，  
    所以"旧库 + 新代码"的典型症状是"应用启动正常、一打开设备页整页 500  
    （`no such column: devices.group_id`）"。`prepare_database` 现在会把这种情况变成  
    一个启动期报错，并直接给出缺哪些列、该怎么修（`Database.missing_schema()`）。
23. **"永远不会有下次执行"的策略在创建时就拒绝**（P2）：cron 与执行窗口没有交集  
    （窗口 02:00–02:05 配 `0 3 * * *`）、或 cron 的日期根本不存在（`0 0 31 2 *`）时，  
    策略会被"建成功"、界面显示"下次执行：-"，然后**永远不跑**——等到有人察觉已经过去  
    几周。`schedules._next_run_or_fail` 把它变成 422 并说清原因。
24. **可订阅的事件必须真的会被产生**（P2）：`device_failed_streak` 曾经只作为消息正文  
    里的一段文字存在、从不进 `events`，于是"只订阅它"的渠道永远收不到消息——界面上  
    能选、实际不生效的触发规则比没有更糟（运维会以为有人在盯）。
25. **业务错误码只对机器人平台解释**（P2）：钉钉/企微/飞书返回 HTTP 200 + `errcode`，  
    必须再看一层（否则"被限流/机器人被移出群"会被记成发送成功）；而**通用 webhook**  
    的响应体格式由对方决定，`{"code": 200}` 这类"用 code 表示成功"很常见，一律按  
    "非 0 即失败"会把已经投递成功的通知记成失败（`_post_json(check_business_code=)`）。

## 新增一个厂商

1. 在 `app/collector/drivers/` 新增 `<vendor>.yaml`：提示符正则、命令表、  
   归一化、脱敏、校验规则、`confirm_prompts`（照抄现有文件改即可）。
2. 在 `tests/fixtures/<vendor>/<os>/` 放真实回显样本（脱敏）。
3. 在 `tests/mock_device/scenarios.yaml` 加一个剧本。
4. 跑 `python -m pytest`，把 `test_drivers.py` 里的参数化用例补上该厂商。
5. 只有当 YAML 描述不了时，才写 Python 子类（见 `drivers/custom_example.py`）。

## 排障

| 现象                                            | 原因 / 处理                                                                                                                                          |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ModuleNotFoundError: No module named 'yaml'` | 依赖没装或被装到了另一个解释器。`pip install -e ".[dev,web]"`，并用 `python -c "import sys; print(sys.executable)"` 确认解释器；测试改用 `python -m pytest`                   |
| 接口全 401                                       | 令牌过期或没带 `Authorization: Bearer <token>`；默认有效期 12 小时                                                                                              |
| 启动后不知道管理员口令                                   | 只在**首次建库**时打印一次。忘了就删库重建，或设 `DSZOON_ADMIN_PASSWORD` 后重建                                                                                           |
| 凭据解密失败                                        | `secret_key` 与写入时不一致（换过密钥文件）。恢复原密钥，或重新录入设备密码                                                                                                     |
| 备份任务卡在 queued                                 | 单进程事件循环被阻塞，或进程重启导致内存里的任务丢了（P2 用持久化队列解决）                                                                                                          |
| SSE 前端收不到进度                                   | 反向代理缓冲了响应体。Nginx 需要 `proxy_buffering off;`（代码已发 `X-Accel-Buffering: no`）                                                                         |
| 测试报"型号对不上 / 中文描述找不到"                          | 大概率是 `tests/fixtures/<vendor>/<tag>/` 被真机探针的输出覆盖了（那批文件是 mock 设备的回显来源）。用 `git checkout tests/fixtures` 恢复，或给真机夹具换 `--tag`                         |
| 探针提示"夹具已存在，未覆盖"                               | 预期行为。确认要替换加 `--force`，或换 `--tag`                                                                                                                 |
| 采集报 `integrity_error`                         | 内容是残的（截断/折行/命令报错），内容仍会归档，看 `report.md` 里的 issue 列表                                                                                               |
| 定时策略到点没执行                                     | ① 策略是否启用、`next_run_at` 是否为空（停用会置空）；② 调度器是否被 `DSZOON_SCHEDULER_ENABLED=false` 关掉；③ 上一轮还没跑完 → `last_status=skipped_busy`；④ 执行窗口把时刻过滤掉了（界面"预览"能给答案） |
| 策略显示"上次结果：发起失败"                               | 范围解析不出设备（设备被删/全停用/站点写错）。点"立即执行"会直接给出 422 的具体原因                                                                                                   |
| 通知发不出去                                        | 在渠道页点"测试"，错误原因会写在渠道的"最近发送"里（也会进发送记录）。钉钉加签、企微/飞书 key 都在 URL 里；SMTP 587 端口必须选 STARTTLS                                                             |
| 清理把版本删多了                                      | 检查 `keep_min_versions`（每台至少保留几个）与 `keep_unchanged_daily_days`；"变更版本保留"留空 = 永久。清理结果与操作人在审计里可查                                                     |
| 启动报"数据库里没有 users 表"                           | 生产模式（`DSZOON_AUTO_CREATE_SCHEMA=false`）下没有先跑 `alembic upgrade head`                                                                              |
| 启动报"数据库 schema 比代码旧：缺少 devices.group_id…"     | `create_all` 只补建缺失的表、**不给已有表加列**。开发环境删掉旧库文件重启（`var/dszoon.db`，但**保留 `.secrets.json`**，删了已录入的设备密码就解不开了）；生产环境跑 `alembic upgrade head`              |
| `alembic upgrade head` 没建任何表                  | 版本目录里没有迁移文件，或基线被删。`python -m pytest tests/unit/test_migrations.py` 能直接验证                                                                         |

## 已知限制（P2 之后仍存在）

- **Telnet 未实现**：`create_session` 会显式抛 `UnsupportedProtocol`，不会静默降级。
- **跳板机（ProxyJump）未实现**：`SessionConfig.proxy_jump` 已预留字段。
- **华三/锐捷尚未真机实测**：见 `../docs/SPIKE_REPORT.md`，命令表需按实测校准。
- **单进程任务队列**：进程重启会丢"排队中/运行中"的任务（记录留在库里，状态不会  
  自动收敛）。多实例部署时同一时刻只应有一个实例开调度器  
  （`DSZOON_SCHEDULER_ENABLED=false` 关掉其余实例）。
- **配置下发（deploy）未开放**：驱动接口已预留 `deploy()`，当前实现显式抛  
  `DeployNotSupported`——v1.0 是只读备份工具。
- **审计查询界面未做**：审计记录从 P1 起就在写（`audit_logs`），但界面在 P3。
- **通知渠道未做分级/静默窗口**：只支持"订阅事件 + 失败台数阈值"，没有值班表、  
  升级通知、维护窗口静默。
- **paramiko 兜底会话**的兼容能力取决于 paramiko 版本；若协商仍失败，  
  需要在设备上单独放开算法白名单（`SessionConfig.legacy_algorithms`）。
