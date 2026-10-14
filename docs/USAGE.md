# dszoon 完整使用过程

从装好到"日常怎么用"的一条走通路径。命令都是可复制执行的（Windows PowerShell；
Linux 上把 `Invoke-RestMethod` 换成 `curl` 即可）。

**先读这三句，能省掉大半困惑：**

1. **对设备只读。** 系统只登录设备执行查看命令，不下发任何配置。
2. **界面覆盖到 P2。** 设备/任务/定时/分组/通知/保留/用户有页面；
   **合规、审计、检索、看板聚合、变更与回滚目前只有接口**，用本文的 API 命令或
   `http://127.0.0.1:8000/docs`（OpenAPI 交互页）操作。对应页面还没做。
3. **配置下发默认关闭，且本版本只能"预演"。** 真实下发执行器（P4-A）尚未接入，
   `/changes/{id}/preview` 会完整跑一遍闸门但一行都不发。所以第 11、12 步是
   **审批流程演练**，不是真的改设备。

---

## 0. 起服务（一次性）

```powershell
cd F:\dszoon\backend
.venv\Scripts\activate
pip install -e ".[dev,web]"          # 首次或升级后
python scripts/preflight.py          # ⓿ 10 秒只读体检：依赖/旧库/前端产物/导入链
python -m app.cli init-db            # 开发环境建表（生产用 alembic upgrade head）
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

界面：`http://127.0.0.1:8000`（后端直接挂载 `web/dist`）。
交互式 API 文档：`http://127.0.0.1:8000/docs`。

> 首次启动若报 `数据库 schema 比代码旧：缺少 …`，说明沿用了旧版本的库：
> `create_all` 只补建缺失的表、**不给已有表加列**。开发库直接删掉重建
> （`Remove-Item var\dszoon.db`，**保留 `var\.secrets.json`**）。

**登录凭据**：`admin` + `DSZOON_ADMIN_PASSWORD`；没设过就是**首次建库那一次启动日志**里
打印的随机口令（只打印一次）。忘了就重置：

```powershell
python -m app.cli reset-password admin --password "新的强口令"
```

下面所有 API 命令都假设你已经有令牌：

```powershell
$Base = "http://127.0.0.1:8000/api/v1"
$tok = (Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType application/json `
        -Body '{"username":"admin","password":"你的口令"}').access_token
$H = @{ Authorization = "Bearer $tok" }
```

**第一件事**：登录后立刻改口令（界面右上角，或 `POST /auth/change-password`）。
初始口令是明文出现在启动日志里的。

---

## 1. 添加设备

设备密码是**加密存储**的（Fernet），任何接口都不返回密码——连密文都不返回。
多台设备可以共用一份凭据档案。

```powershell
# ① 凭据档案（只读账号！设备侧不要给它配置权限）
$cred = Invoke-RestMethod -Method Post "$Base/credentials" -Headers $H -ContentType application/json -Body (@{
  name = "核心区只读"; username = "netadmin"; password = "设备口令"
} | ConvertTo-Json)

# ② 设备台账
$dev = Invoke-RestMethod -Method Post "$Base/devices" -Headers $H -ContentType application/json -Body (@{
  name = "HW-CORE-01"; host = "10.0.0.1"; vendor = "huawei"      # vendor: huawei | h3c | ruijie
  credential_profile_id = $cred.id
  group_id = $null                                                # 可选：先建分组再填
} | ConvertTo-Json)

# ③ 先测连通性，再采集（预检能提前挡住"账号错/端口不通/关不掉分页"）
Invoke-RestMethod -Method Post "$Base/devices/$($dev.id)/test" -Headers $H | ConvertTo-Json
```

批量导入用 `POST /devices/import`（JSON 行，逐行报错、不因一行坏掉整批失败）。
支持的厂商别硬记：`GET /meta/vendors` 会列出驱动名、别名与它实际会发的命令。

---

## 2. 第一次采集

```powershell
# 发起（也可用 group_id / site / vendor / all 圈范围，见 /docs 的请求 schema）
$task = Invoke-RestMethod -Method Post "$Base/backups" -Headers $H -ContentType application/json -Body (@{
  device_ids = @($dev.id); name = "首次采集"
} | ConvertTo-Json)

# 看进度（SSE，浏览器/curl -N 都行；界面上是进度条）
# GET $Base/backups/$($task.id)/stream

# 看结果
Invoke-RestMethod "$Base/backups/$($task.id)" -Headers $H | ConvertTo-Json -Depth 6
```

要理解三个词，它们在整套系统里反复出现：

| 词 | 含义 |
|---|---|
| **run（采集记录）** | 一次**尝试**。成功、失败、超时都留痕，每台设备每次一条 |
| **snapshot（版本）** | 一份**内容**。内容寻址：同一份配置采 100 次也只有一行、一个文件 |
| `unchanged` | 采到了，但和上一版一模一样。这是**好结果**，不是失败 |

所以"为什么我的采集记录很多、版本很少"是正常的。

---

## 3. 看版本与对比

```powershell
# 版本列表（最新在前）
Invoke-RestMethod "$Base/devices/$($dev.id)/snapshots" -Headers $H | ConvertTo-Json -Depth 4

# 某一版的元信息 / 正文（正文支持 format 与 download=1）
Invoke-RestMethod "$Base/snapshots/1" -Headers $H | ConvertTo-Json
Invoke-RestMethod "$Base/snapshots/1/content" -Headers $H

# 版本对比（留空 = 与倒数第二版比 / 取最新版）
Invoke-RestMethod "$Base/devices/$($dev.id)/diff" -Headers $H | ConvertTo-Json -Depth 4
```

界面上对应"设备详情 → 版本与对比"，能直接看 diff、看正文、下载配置。
配置正文存在 `<数据目录>\objects\<sha前2位>\<sha>.cfg`，数据库里只有指针——
**备份时必须连数据目录一起备**，只备数据库等于只有目录没有内容。

---

## 4. 定时与保留

```powershell
# 每天 02:00 采一次（cron 按容器/主机的本地时区算，注意 TZ）
$sch = Invoke-RestMethod -Method Post "$Base/schedules" -Headers $H -ContentType application/json -Body (@{
  name = "每日 02:00"; cron = "0 2 * * *"; enabled = $true
  device_ids = @($dev.id)                 # 也可用 group_id / site / vendor / all
} | ConvertTo-Json)

# 下次什么时候跑（这条能立刻发现"执行窗口与 cron 无交集"这类配错）
Invoke-RestMethod "$Base/schedules/$($sch.id)/preview?count=5" -Headers $H | ConvertTo-Json

# 立即跑一次（不会改变已排定的下次执行时间）
Invoke-RestMethod -Method Post "$Base/schedules/$($sch.id)/run" -Headers $H | ConvertTo-Json

# 保留策略
Invoke-RestMethod "$Base/retention" -Headers $H | ConvertTo-Json
Invoke-RestMethod -Method Put "$Base/retention" -Headers $H -ContentType application/json `
  -Body '{"enabled":true,"keep_unchanged_daily_days":90,"keep_min_versions":2}' | ConvertTo-Json
Invoke-RestMethod -Method Post "$Base/retention/run" -Headers $H | ConvertTo-Json
```

保留策略的自动清理挂在调度器轮询里（`DSZOON_RETENTION_INTERVAL_HOURS`，默认 24 小时）——
所以 **`DSZOON_SCHEDULER_ENABLED=false` 时不会自动清理**，只能手工点"立即清理"。
别把它当默认关闭的东西：只靠人点的清理在真实环境里等于永远不跑，版本会堆到磁盘满。

---

## 5. 合规检查（接口/`/docs`）

内置基线 16 条（8 high / 7 medium / 1 low）。采集到新版本后会自动评估
（`DSZOON_COMPLIANCE_ENABLED`，默认开）。

```powershell
Invoke-RestMethod "$Base/compliance/rules" -Headers $H | ConvertTo-Json -Depth 4   # 跑的是哪一版规则
Invoke-RestMethod "$Base/compliance/summary" -Headers $H | ConvertTo-Json          # 合规概况
Invoke-RestMethod "$Base/compliance/report" -Headers $H | ConvertTo-Json -Depth 4  # 设备 × 规则矩阵
Invoke-RestMethod -Method Post "$Base/compliance/run" -Headers $H -ContentType application/json `
  -Body '{"all_enabled":true}' | ConvertTo-Json
Invoke-RestMethod "$Base/compliance/export?fmt=csv" -Headers $H -OutFile report.csv # 导出本身也写审计
```

报表里 `verdict` 有四种：`pass` / `warn` / `fail`（有 high 级违规）/ `unknown`（还没查过）。
**`unknown` 必须单独看**——"还没查"和"查了没问题"是两件事。

换成自有基线：`DSZOON_COMPLIANCE_RULES='["/etc/dszoon/rules.yaml"]'`（**整体替换**，不是叠加），
改完 `POST /compliance/cache/reset` 重新加载。

---

## 6. 通知渠道

```powershell
Invoke-RestMethod "$Base/notification-events" -Headers $H | ConvertTo-Json   # 可选事件
$ch = Invoke-RestMethod -Method Post "$Base/channels" -Headers $H -ContentType application/json -Body (@{
  name = "运维群"; kind = "dingtalk"                                          # smtp/webhook/dingtalk/wecom/feishu
  config = @{ webhook = "https://oapi.dingtalk.com/robot/send?access_token=..." }
  events = @("task_failed", "integrity_error", "compliance_failed")
} | ConvertTo-Json -Depth 5)
Invoke-RestMethod -Method Post "$Base/channels/$($ch.id)/test" -Headers $H | ConvertTo-Json
Invoke-RestMethod "$Base/notification-logs" -Headers $H | ConvertTo-Json -Depth 4
```

机器人地址是**凭据**：接口返回时会打码，编辑时回填打码值不会把真实地址覆盖掉。
通知发送失败**不会**拖累备份——"通知坏了"永远不该让人以为"备份坏了"。

---

## 7. 审计与检索（接口/`/docs`）

审计从第一天就在记：登录、凭据增删改、设备增删改、发起备份、下载配置、查看明文、
合规执行与导出、以及 P4 的每一步变更动作。

```powershell
Invoke-RestMethod "$Base/audit-logs?limit=50" -Headers $H | ConvertTo-Json -Depth 5
Invoke-RestMethod "$Base/audit-logs/actions" -Headers $H | ConvertTo-Json           # 动作字典
Invoke-RestMethod "$Base/audit-logs/downloads" -Headers $H | ConvertTo-Json -Depth 4 # 谁下载过配置
Invoke-RestMethod "$Base/audit-logs/export?fmt=csv" -Headers $H -OutFile audit.csv  # 导出本身也留痕
```

跨设备检索配置（**需要 auditor 及以上，且每次搜索都写审计**）：

```powershell
# 在哪台设备的最新版本里还有这段配置？（在原文上匹配，返回的是脱敏行）
Invoke-RestMethod "$Base/search/config?q=snmp-agent%20community&limit=50" -Headers $H | ConvertTo-Json -Depth 5
```

它**不是全文检索引擎**：不建索引、不做分词，每次读盘，所以有 `max_devices` 上限，
结果里会如实告诉你 `scanned_devices` / `matched_devices` / `truncated`。
看到 `truncated: true` 就别下"全网只有这一台"的结论。

---

## 8. 看板

```powershell
Invoke-RestMethod "$Base/meta/overview" -Headers $H | ConvertTo-Json -Depth 5
```

一次取齐：设备（按厂商）、版本（近 7 天变更次数）、采集（近 24h 各状态 + 成功率）、
合规（各判定 + `unknown`）、变更单（按状态）、存储占用、最近 5 条采集。

两个容易误读的地方：**没有采集数据时 `success_rate_7d` 是 `null` 而不是 0**
（0% 会让看板一上来就是红的）；**`unchanged` 算成功**（配置没变不等于采集失败）。

---

## 9. 变更单与审批（P4，接口/`/docs`）

这是"改配置"这件事的**审批流程**。默认关闭（`DSZOON_DEPLOY_ENABLED=false`），
且本版本只能预演。

```powershell
Invoke-RestMethod "$Base/deploy/status" -Headers $H | ConvertTo-Json   # 本环境是否开启

# ① 提单（草稿）。lines 可以传数组，也可以直接粘一段多行文本
$chg = Invoke-RestMethod -Method Post "$Base/changes" -Headers $H -ContentType application/json -Body (@{
  device_id = $dev.id
  lines = @("interface GigabitEthernet0/0/1", "description uplink-to-core")
  note = "对端扩容，加描述"
} | ConvertTo-Json)

# ② 提交审批（提交前会再过一遍预检）
Invoke-RestMethod -Method Post "$Base/changes/$($chg.id)/submit" -Headers $H -ContentType application/json -Body '{}' | ConvertTo-Json

# ③ 审批——★ 必须由**另一个人**来做（申请人自批会被拒：409 self_approval）
$op = (Invoke-RestMethod -Method Post "$Base/auth/login" -ContentType application/json `
      -Body '{"username":"approver","password":"他的口令"}').access_token
$Hop = @{ Authorization = "Bearer $op" }
Invoke-RestMethod -Method Post "$Base/changes/$($chg.id)/decision" -Headers $Hop -ContentType application/json `
  -Body '{"approve":true,"note":"同意"}' | ConvertTo-Json
```

几件**必须知道**的规则（它们都有测试盯着）：

| 规则 | 为什么 |
|---|---|
| 危险命令（`save`/`reboot`/`reset`/`erase`/整机 `shutdown`…）**在创建时就被拒**，`save` 也不例外 | 下发场景下"保存当前配置"会把半成品写进启动配置；这类命令一旦发出去无法撤回 |
| 审批绑定**内容哈希**：批准之后改一个字，原审批立刻作废（转"审批已失效"），必须重新提交 | 否则就成了"批的是 A、发的是 B" |
| 申请人**可以**自己执行，但**不能**自己审批 | 现场常态是"申请人提单 → 主管批准 → 申请人执行"；必须分开的只有审批权 |
| 改内容要用 `PATCH /changes/{id}/lines`；待审批的单子要先撤回再改 | 避免"审批人看的是旧内容、点的是新内容" |

---

## 10. 下发前的基线、预演（照旧不发命令）

```powershell
# ④ 先备份（§8.5.1 第 3 条：先备份后下发，没有基线不许发）
$task2 = Invoke-RestMethod -Method Post "$Base/backups" -Headers $H -ContentType application/json `
  -Body (@{ device_ids = @($dev.id); name = "变更前基线" } | ConvertTo-Json)
$snap = (Invoke-RestMethod "$Base/devices/$($dev.id)/snapshots" -Headers $H).items[0].id

# ⑤ 把这次采集登记为"下发前基线"（必须在提单**之后**采，否则会被判 stale_baseline）
Invoke-RestMethod -Method Post "$Base/changes/$($chg.id)/baseline" -Headers $H -ContentType application/json `
  -Body (@{ snapshot_id = $snap } | ConvertTo-Json) | ConvertTo-Json

# ⑥ 预演：完整跑一遍闸门，但一行都不发、也不改变更单状态
Invoke-RestMethod -Method Post "$Base/changes/$($chg.id)/preview" -Headers $H | ConvertTo-Json -Depth 6
```

预演返回的 `blockers` 就是**还差什么才能发**，可能的取值：

| code | 含义 |
|---|---|
| `deploy_disabled` | `DSZOON_DEPLOY_ENABLED=false`（默认） |
| `not_approved` | 还没批准 |
| `no_baseline` / `stale_baseline` | 没有下发前基线 / 基线比变更单还早 |
| `content_changed` | 待下发内容与审批时的哈希不一致 |
| `precheck_failed` | 内容本身没过预检（危险命令等） |
| `no_approver` / `self_approved` / `confirm_required` | 审批信息不完整 |

`blockers` 为空 = 满足下发条件。**但本版本仍然不会真的发出去**——
没有真实下发路由，这是刻意的：留一条"看起来能发、实际发得不对"的路由比没有更危险。

---

## 11. 一键回滚到上一个可用版本（P4，接口/`/docs`）

```powershell
# 先看：目标是哪个版本、跳过了谁、为什么、要补什么、还差什么人工处理
Invoke-RestMethod "$Base/devices/$($dev.id)/rollback/preview" -Headers $H | ConvertTo-Json -Depth 6

# 生成回滚变更单（草稿）——它照样要审批、要基线、只能预演，没有"紧急通道"
Invoke-RestMethod -Method Post "$Base/devices/$($dev.id)/rollback" -Headers $H -ContentType application/json `
  -Body '{"note":"恢复接口配置"}' | ConvertTo-Json -Depth 6
```

"上一个**可用**版本"不是"上一个版本"：内容残缺（截断/折行/报错回显）的版本、
以及 `unchanged` 的那些，都会被跳过——`preview` 里 `skipped` 会**逐个列出原因**，
所以看到"目标不是最新那一版"时先看 skipped，不要以为工具漏了。

回滚内容有两条硬规则：

- **只自动补回"目标有、当前没有"的内容，并且整块重发**（连 `interface` 这样的视图入口一起发，
  否则 `description xxx` 会落到别的视图里）；
- **删除类一律不自动生成**——删一行要写 `undo`/`no`，各家写法不同、还得先进入对应视图，
  猜错就是改坏设备。这类差异会返回 422 并列出 `needs_manual`（要人工处理的具体行）。

所以真实世界最常见的变更（"有人加了一行"）会得到 **0 行可自动下发内容 + 一份待人工删除清单**，
这是设计如此，不是失败。

---

## 12. 日常巡检（建议节奏）

| 频率 | 动作 |
|---|---|
| 每天 | 看 `GET /meta/overview` 的 `runs.failed_last_24h` 与 `changes` 里待审批的条数 |
| 每周 | 抽查 2~3 台设备的 `diff`（有没有人偷偷改了配置）；看磁盘占用 |
| 每月 | 抽查恢复：从数据目录取一份历史配置，确认能打开、内容完整 |
| 每季度 | 复核台账（下线设备停用）、复核保留策略是否与制度一致 |

监控接入：`GET /api/v1/metrics`（Prometheus 文本，**不要求登录**，别暴露到公网）。
最值得告警的一条是 `dszoon_schedules_due_total > 0` 持续 10 分钟——它说明**调度器没在跑**。

---

## 13. 出问题时

```powershell
python scripts/preflight.py    # ⓿ 10 秒只读：导入链/依赖/旧库/前端产物/前端导入一致性
python scripts/selfcheck.py    # ① 分层自检：环境→语法→导入→清洗→建库→端到端备份→运营链路
python scripts/smoke.py        # ② 冒烟子集（35 项），失败能定位到子系统
python -m pytest -q            # ③ 完整回归
```

常见现象与处置见 **`docs/DEPLOYMENT.md` 第 16 节（故障处置表）**——
那里按报错原文收录了"启动报 schema 比代码旧""界面只有 API、404""改了 .env 但容器里不生效"
"定时任务到点不跑""任务进度条不动"等等，附原因与处置。
