# 网络设备配置备份系统 —— 开发计划

> 目标：基于 Python 开发一套 Web 交互的交换机/路由器配置备份系统，原生支持华为（VRP）、华三（Comware）、锐捷（RGOS），并可低成本扩展到 Cisco / 中兴 / 烽火等。
>
> 文档状态：v0.1 计划稿，待确认需求后细化。

---

## 1. 范围界定

### 1.1 做什么（In Scope）

| 能力 | 说明 |
|---|---|
| 设备管理 | 设备台账（IP、厂商、型号、软件版本、站点、分组、标签、凭据） |
| 配置采集 | SSH/Telnet 登录 → 关闭分页/换行 → 拉取 running-config → 落库归档 |
| 版本管理 | 不可变快照（内容寻址 sha256）+ 版本链路 + 变更 diff |
| 定时任务 | 按分组/全网调度，错峰、并发控制、失败重试、断点续采 |
| Web UI | 登录、设备 CRUD、一键备份、进度实时日志、配置查看/下载/diff/回滚版本对比 |
| 通知告警 | 备份失败、配置变更、基线不合规 → 邮件/Webhook/钉钉/企微/飞书 |
| 合规检查 | 金标准基线 + 规则引擎（YAML 规则），出报告 |
| 权限审计 | RBAC（admin/operator/auditor/viewer）+ 全量操作审计日志 |

### 1.2 明确不做 / 后置（Out of Scope for v1.0）

- **配置下发与回滚**（Restore/Deploy）：风险高。**v1.0 只做「备份 + 只读展示」，但架构上预留接口**——`VendorDriver.deploy()` 的签名已定（含 `dry_run` 语义与"任一行报错即中止"的约束），全部驱动默认抛 `DeployNotSupported`；P4 引入时以独立特性、审批流、diff 预览的方式落地，不重构采集引擎。
- 网络拓扑自动发现与画图（可复用 LLDP/CDP 采集做轻量版）。
- 设备性能监控（CPU/内存/流量曲线），交给 Zabbix/Prometheus 体系。

> 实现进度：P0 骨架已落地在 `backend/`（采集层 + 三家驱动 + mock 设备 + 夹具 + 测试），
> 详见 `backend/README.md`；真机校准结果填写到 `docs/SPIKE_REPORT.md`。
> **P1 已交付**：后端 API（`backend/app/`，7 张表 + 鉴权/RBAC + 备份任务与 SSE + 版本对比）、
> 前端界面（`backend/web/`，Vue3 + Element Plus）、部署文件（`backend/deploy/`，
> Docker/compose/nginx/systemd/离线包）。前端放在 `backend/web/` 而不是根目录的
> `frontend/`：后端 `app/main.py` 会自动挂载 `web/dist`，同端口提供前后端，
> 部署时少一层反向代理配置。
> **P1 的验收步骤与失败排查见 `docs/P1_RUNBOOK.md`。**
>
> **P3 运维增强（进行中）**：
>
> | 交付项 | 落点 | 状态 |
> |---|---|---|
> | 合规基线引擎（YAML 规则 + 逐行评估 + verdict 合成 + 违规行脱敏入库） | `backend/app/compliance/`、`backend/app/services/compliance.py` | 已交付 |
> | 内置基线 16 条（三家厂商语法差异、16 条规则里含 8 条 high） | `backend/app/compliance/baselines/baseline.yaml` | 已交付 |
> | 采集成功后自动评估 + 结果留存历史 | `backend/app/services/backups.py::_check_compliance`、`compliance_results` 表 | 已交付 |
> | 报表接口（设备×规则矩阵、合规率按厂商/站点、CSV/JSON 导出并写审计） | `backend/app/api/v1/compliance.py` | 已交付 |
> | 单元测试（引擎语义 + 三份真实回显的逐条预期） | `backend/tests/unit/test_compliance.py` | 已交付（待运行） |
> | 不合规通知事件 `compliance_failed` | — | 待做 |
> | 审计日志查询/动作字典/导出（CSV 带 BOM）+ 配置下载专用视图（等保那一问） | `backend/app/api/v1/audit.py`、`schemas/audit.py` | 已交付 |
> | 审计接口测试（下载三要素、权限、导出留痕） | `backend/tests/api/test_audit.py` | 已交付（待运行） |
> | 重要变更**分级识别**：10 类关注清单各带 high/medium，含删除侧（删 ACL 也是高危） | `backend/app/services/diffs.py::classify_change` | 已交付 |
> | 分级识别单测（新增/删除/去重/封顶/等级排序） | `backend/tests/unit/test_diffs_risk.py` | 已交付（待运行） |
> | Prometheus `/metrics`（设备/版本/任务/采集/调度积压/合规/通知 + up） | `backend/app/services/metrics.py`、`api/v1/metrics.py` | 已交付 |
> | 指标渲染单测（格式合规、标签转义、NaN/Inf 归零、HELP 覆盖） | `backend/tests/unit/test_metrics.py` | 已交付（待运行） |
> | 风险等级落到 run 上 + `risky_change` 通知事件 | — | 待做（依赖给 `backup_runs` 加两列） |
> | `compliance_failed` 通知事件（只对**本次任务期间**判定的不合规告警，避免反复刷） | `backend/app/services/notifications.py` | 已交付 |
> | 合规接口层测试 6 条（未评估设备、立即检查、导出留痕、单设备视图、通知触发、不重复告警） | `backend/tests/api/test_compliance.py` | 已交付（待运行） |
>
> **P3 的验收步骤、期望结果与失败对照表见 `docs/P3_ACCEPTANCE.md`**（含"仍未做"的四项，
> 避免把未交付误读成已交付）。

---

## 8.5 P4 高级特性：工作分解与安全设计（**动手前先读完本节**）

P4 与前三阶段的**风险等级不同**：P0~P3 只读设备，最坏情况是"没采到"；P4 要往生产设备
写配置，最坏情况是**把网络改坏**。所以这一节的顺序是"先定不可逾越的约束，再谈功能"。

### 8.5.1 五条不可妥协的安全约束（对应 `VendorDriver.deploy()` 的既有签名）

1. **默认关闭、且默认 dry_run**：`DSZOON_DEPLOY_ENABLED=false` 时 `deploy()` 一律拒绝；
   接口层再加一道"必须显式传 `confirm=true` + 申请人以外的第二人审批通过"。
2. **任一行报错即中止，且中止后不再发送任何命令**：设备配置是"部分生效"最危险——
   一半新一半旧的状态比两个完整状态都难排查。引擎必须逐行发送、逐行确认回显，
   遇到 `Error:`/`% Invalid`/`% Unrecognized` 立刻停止并记录"已生效到第 N 行"。
3. **先备份后下发**：下发前强制对该设备做一次采集；采不到就不允许下发
   （否则回滚的基线不存在）。
4. **回滚用"上一个可用版本"，不用"上一个版本"**：可用 = 采集成功且 `integrity` 通过
   且被判为 `success`（不是 `unchanged`/`integrity_error`）。回滚本身也走同一套
   审批 + dry_run + 逐行校验流程，不搞"紧急通道"。
5. **全部动作进审计，且审计里带 `before/after` 快照 id**：谁在何时对哪台设备下发了
   哪个版本、审批人是谁、结果如何——这是 P4 唯一能被事后追责的依据。

### 8.5.2 工作分解

| 子阶段 | 交付内容 | 落点（预计） |
|---|---|---|
| **P4-A 下发引擎** | `deploy` 在驱动层落地（华为 `system-view` 进卷积、逐行 + `commit`；锐捷/华三同构）；`dry_run` 只做语法预检与回显模拟；`DeployNotSupported` 保留给未实现的厂商 | `app/collector/drivers/*.yaml`（新增 `deploy_commands`）、`app/collector/session.py`、`app/collector/engine.py` |
| **P4-B 变更单与审批流** | 数据模型：`change_requests`（目标设备/目标版本/diff/申请人/审批人/状态/时间线）、`deploy_runs`（逐行结果与回显）；API：创建 → 预览 diff → 审批 → 下发 → 回滚 | `app/db/models.py`、`app/services/deploy.py`、`app/api/v1/deploy.py`、`app/schemas/deploy.py` |
| **P4-C 回滚** | "一键回滚到上一个可用版本"：从合规/完整性两个维度挑版本，自动生成 diff 预览与变更单 | `app/services/deploy.py::rollback_target` |
| **P4-D 横向扩展** | Celery + Redis：任务队列从进程内挪到 broker；SSE 改成 Redis pub/sub；调度器加数据库行锁，多实例安全 | `app/services/backups.py` 的 `EventBus`/`handle` 抽象、`deploy/` 增加 worker 服务 |
| **P4-E 接入与发现** | LDAP/AD/SSO 登录（保留本地 admin 兜底）；SNMP 自动发现（只读抓 sysName/sysDescr/邻居，生成"待纳管"清单，**不自动入库**）；Git 后端存储（blob → 仓库提交，历史即提交历史） | `app/core/security.py`、`app/services/discovery.py`、`app/services/blobs.py` 的存储后端抽象 |

### 8.5.3 顺序与前置条件

1. **前置**：P1~P3 全量测试必须**先确认全绿**（当前 P3 最后一次冒烟有 2 处失败，已修但
   未重跑）。P4 写的是生产设备，底座不能是"大概没问题"。
2. 顺序建议：**P4-A → P4-B → P4-C**（下发/审批/回滚是一条链，必须一起可用才算交付）；
   P4-E 的 LDAP 与 SNMP 发现可并行；P4-D 放最后（它会动到 P0/P1 的并发与事件总线，
   没有真实压测结论前不碰）。
3. 验收：**"一键回滚到上一个可用版本"在灰度环境验证通过**（计划 §8 的 P4 验收标准）；
   另需一份"下发失败后设备处于什么状态、如何人工接管"的操作手册。

### 8.5.4 P4 明确不做

* 不做"批量下发到全网"（一条变更单只对明确圈定的设备生效，且数量上限可配）。
* 不做"自动修复不合规"（P3 的合规结果只作提示，不自动生成下发内容——自动改生产配置
  是把工具变成事故源的最短路径）。
* 不做配置模板/变量渲染（那是编排系统的职责范围，不在备份工具的边界内）。
> | 合规接口层测试、前端页面、全文检索、Prometheus 指标 | — | 待做 |
>
> **P2 已交付（代码完成，待人工执行验证）**：
>
> | 交付项 | 落点 |
> |---|---|
> | 定时调度：cron / 固定间隔 + 错峰抖动 + 执行窗口 + 范围（设备/分组/站点/厂商/全部）+ 任务关联 | `backend/app/services/schedules.py`、`scheduler.py` |
> | 保留策略与清理执行器：版本折叠、正文回收、会话日志清理 | `backend/app/services/retention.py`、`blobs.py` |
> | 通知渠道：邮件/Webhook/钉钉/企微/飞书 + 触发规则（事件 + 失败台数阈值）+ 发送记录 | `backend/app/services/notifications.py` |
> | 设备分组（定时任务按组圈范围） | `backend/app/services/groups.py` |
> | 运营接口与前端页面（分组/定时策略/通知渠道/保留策略） | `backend/app/api/v1/ops.py`、`backend/web/src/views/` |
> | 迁移基线 + 漂移测试 | `backend/migrations/versions/0001_initial_schema.py` |
> | 1000 台压测脚本与规模化测试 | `backend/scripts/loadtest.py`、`backend/tests/api/test_scale.py` |
>
> P2 的关键设计取舍（轮询数据库而非内存 job、错过的班次不补跑、手工触发不改班次、
> 保留策略默认保守、机器人地址打码、生产缺表直接报错）写在 `backend/README.md`
> 的"设计要点"第 13~25 条，复核与验证步骤见 **`docs/P2_ACCEPTANCE.md`**。

### P2 交接说明（截至本会话结束）

**⚠ 状态：代码已写完，但一行都没有在真实解释器里运行过。**
开发环境里 shell 进程启动即失败（退出码 `0xC0000142`），本会话无法执行任何命令；
所有运行期验证都只能由人在能跑的机器上完成。P2 验收标准里"保持 P1 的 156 个用例全绿"
因此**尚未被证实**。

**待执行的命令（按顺序，约 5 分钟）**

```powershell
cd F:\dszoon\backend
.venv\Scripts\activate
python scripts/preflight.py     # ⓿ 10 秒只读体检：导入链/依赖/旧库/前端产物
pip install -e ".[dev,web]"     # ★ P2 新增 apscheduler，不装则整个 API 层导入即失败
Remove-Item var\dszoon.db       # ★ 现存的是 P1 老库（体检会告诉你要不要删；保留 .secrets.json）
python scripts/selfcheck.py     # ① 8 步逐级（第 8 步是 P2 运营链路，发送环节打桩）
python scripts/smoke.py         # ② P2 冒烟 21 项，失败能立刻定位到子系统
python -m pytest -q             # ③ 全量，期望 280~340 全绿（P1 的 156 个必须仍全绿）
python scripts/loadtest.py --devices 1000 --latency 0.01 --concurrency 32 --rounds 2
cd web; npm run build           # ④ ★ web/dist 是旧产物，不含 4 个新页面
```

**未验证项（明确列出，避免误以为已验过）**

| 项 | 说明 |
|---|---|
| P2 全部后端与前端代码 | 仅静态审查；约 130 个新增测试全部未运行 |
| 应用启动（建表 / 挂载前端 / 调度器生命周期） | 从未跑过 `uvicorn`（现存 `var/` 是 P1 时期留下的） |
| 前端 4 个新页面 | `web/dist` 仍是旧产物，从未构建过含新页面的版本 |
| 1000 台压测 | 脚本未执行过 |
| Alembic 基线迁移 | 未在真实 alembic 上执行过（仅静态比对表/索引） |
| 邮件 / 钉钉 / 企微 / 飞书真实投递 | 需要真实账号与群机器人 |
| 华三 / 锐捷真机 | 见 `docs/SPIKE_REPORT.md` |

**若运行变红，先看哪里**

1. `preflight.py` / `selfcheck.py` 的失败信息通常直接指出层（依赖 / 导入 / 建库 / 逻辑）；
2. `smoke.py` 的 21 项每条压一个子系统，配合 `docs/P2_ACCEPTANCE.md` 步骤 1a 的对照表；
3. `docs/P2_ACCEPTANCE.md` 第 6 节"失败特征对照表"按现象查原因；
4. 全量 pytest 里若出现大量 `ImportError`，先看 `tests/unit/test_package_exports.py`——
   它会一次列出所有漏导出的符号（这类问题会让 pytest 停在收集阶段）。

**本会话自查发现并修掉的问题（都是"功能看着有、实际不生效"型，pytest 全绿也发现不了）**

| # | 问题 | 后果 |
|---|---|---|
| 1 | 渠道类型名写成 `email`，服务层实际是 `smtp` | 所有邮件渠道建立时 422 |
| 2 | `DeviceOut` 没有 `group_id` | 分组"成员"对话框预选为空，一保存就把整组踢出 |
| 3 | `device_failed_streak` 从不进 `events` | "设备连续失败"触发规则永远不会触发 |
| 4 | cron 与执行窗口无交集的策略被静默接受 | 界面显示"下次执行：-"，然后永远不跑 |
| 5 | 通用 Webhook 套用了机器人的"非 0 即失败" | 已投递成功的通知被记成失败 |
| 6 | `POST /backups` 审计漏掉 `group_id` | 按分组发起的备份在审计里"范围为空" |
| 7 | 定时策略的创建/修改审计不记 `scope` | "谁把夜间任务改成全量"查不到 |
| 8 | docker-compose 未透传 P2 开关 | 多实例部署时调度器重复执行同一策略；`AUTO_CREATE_SCHEMA=false` 失效 |
| 9 | 收件人字段被当作数组 spread | 经 API 建的邮件渠道，编辑后收件人变成单个字符 |

守卫测试已就位：`test_channel_kind_literal_matches_service_enum`、
`test_device_failed_streak_event_fires_only_after_repeated_failures`、
`test_schedule_with_impossible_window_is_rejected`、
`test_post_json_generic_webhook_does_not_interpret_code`、
`test_audit_records_the_real_backup_scope`、`tests/unit/test_deploy_config.py`（4 条）等。
它们检查的是"代码与文档/部署/接口语义之间的一致性"——这是 pytest 天然覆盖不到的一层。

---

### P4-B 进度：变更单与审批流的地基（本轮）

**已落地**（纯逻辑 + 数据模型，可被单测穷举）

| 交付项 | 落点 | 说明 |
|---|---|---|
| 变更单状态机 | `backend/app/services/deploy.py` | 9 个状态 + 一张 `CHANGE_TRANSITIONS` 迁移表；`can_transition` 对**未知状态一律 False**（fail closed） |
| 下发闸门 | 同上 | 把 §8.5.1 的五条约束变成可复核的判断：开关关闭 / 未显式确认 / 非已批准 / 无审批人 / 自批 / 哈希不符 / 预检未过 / 无基线 / 基线早于变更单 |
| 审批闸门 | 同上 | 只有「待审批」可批准或驳回；默认**必须第二人审批**（`DSZOON_DEPLOY_ALLOW_SELF_APPROVAL` 默认 false） |
| 结论 → 状态映射 | 同上 | `success/stopped/rejected` → `succeeded/stopped/rejected`；**中止永不追认成功**（映射返回 `None`，调用方没法写库） |
| 两张新表 | `backend/app/db/models.py` | `change_requests`（审批绑定 `content_hash`、`before/after_snapshot_id`、只追加的 `timeline`）、`deploy_runs`（逐行结果、`dry_run`、`applied_lines`、`stopped_at`） |
| 两个新开关 | `backend/app/core/config.py` | `DSZOON_DEPLOY_ENABLED=false`（**默认关闭**）、`DSZOON_DEPLOY_ALLOW_SELF_APPROVAL=false` |
| 单测（纯逻辑） | `backend/tests/unit/test_change_flow.py` | 27 条，专门穷举"真机上永远不会复现"的分支 |
| 数据库编排 | `backend/app/services/changes.py`（新） | 创建/提交/审批/撤回/改内容/登记基线/预演/记录下发。★ JSON 时间线必须**整体重新赋值**——`row.timeline.append(...)` 不写库且完全不报错 |
| 对外契约与路由 | `backend/app/schemas/deploy.py`、`backend/app/api/v1/deploy.py`（新） | 10 条路由。★ **故意没有"真实下发"路由**：它要等 P4-A 的驱动层；留一条"看起来能发、实际发不出去或发得不对"的路由比没有更危险 |
| 接口层测试 | `backend/tests/api/test_deploy_flow.py`（新） | 10 条：权限、状态真落库、审计链、**预演不改状态**、无基线 / 基线太旧 / 开关关闭三种拦截 |

**两个刻意的决定（写下来，防止以后被"顺手优化"掉）**

1. **不禁止申请人执行自己提的变更**。现场常态是"申请人提单 → 主管批准 → 申请人执行"，
   必须分开的只有**审批权**。多禁一层，只会逼出"用别人账号点一下"的绕行，
   追责链反而更假。`test_requester_may_execute_after_second_person_approves` 就是钉它的。
2. **中止后不允许直接重发**。`stopped` 只能回到「待审批」重新审批——设备此刻新旧配置
   混在一起，下一次下发是一次全新的决定，不能拿旧批准当通行证。

**迁移说明（★ 复核过，结论是"本轮不加迁移文件"）**

`migrations/versions/0001_initial_schema.py` 是**用活模型 metadata 建表**的
（`Base.metadata.create_all`），不是手抄 `create_table`。所以新增的两张表会自动包含在
基线里：`test_initial_migration_matches_models` 仍然成立，全新安装的 `alembic upgrade head`
也会建出这两张表。反过来，**现在补一个 0002 会让全新安装撞"表已存在"**。

⚠ 由此留下一笔欠账，触发条件明确：基线**不是冻结的**，它跟着 `Base.metadata` 走。
任何"已经 `alembic upgrade` 过"的库，在下次模型变更时不会自动得到新表/新列；那时必须补
一条独立迁移，而那条迁移又要对新库幂等。**在第一个真实部署实例出现之前**把 0001 冻结成
显式 `create_table` 是干净的做法；在那之前，模型变更不需要迁移文件。

---

### P4-C 进度：回滚内容计算（本轮）

**已落地**：`app/services/deploy.py` 的 `rollback_lines()` + `RollbackPlan`（纯逻辑），
测试 `backend/tests/unit/test_rollback_lines.py`（11 条）。

**两条刻意保守的规则**（每一条都对应一个"会改坏设备"的可能）：

1. **只自动补回"目标有、当前没有"的内容，并且整块重发。** 三家配置都用块分隔行
   （华为/华三 `#`、锐捷 `!`——夹具实测确认），所以"块内缺一行就整块重发（含视图入口行）"
   是一条可复核的规则；把 `description xxx` 单独发出去，它会落到别的视图里。
2. **删除类一律不自动生成。** 删一行要写 `undo xxx` / `no xxx`，还得先进入对应视图，
   同一语义在不同版本上写法都可能不同。离线猜这个等于往生产设备发一段没人验证过的命令
   ——这类差异全部进 `needs_manual`，由人在变更单里补。于是"一键回滚"的语义是清楚的：
   **能自动恢复的是"被删掉/被改坏的内容"，需要人工确认的是"当前多出来的内容"**
   （真实世界里最常见的变更恰恰是"多了一行"，所以这条不是边角情况）。

另外三条细节：`return`/`end`/`quit`/`exit` 与分隔行都不进下发内容（`return` 会让设备退出
配置模式，当场打断逐行确认流程）；没有块分隔行的配置标记 `context_uncertain`；
超过 `MAX_LINES` 标记 `truncated`（§8.5.4 不做整份配置替换）。

**下一步**：接上数据库与路由——候选版本按"**创建它的那次采集**"判定
（`backup_runs.snapshot_id` 取最早那次的状态，并合并所有引用该快照的 run 的 issues，
偏保守），选不出目标时把"跳过了谁、为什么"如实返回；`kind=rollback` 的变更单
仍走同一套审批 + 基线 + 预演闸门，不搞"紧急通道"。

---

### P4 收尾状态（第 46~53 轮的交付与未验证清单）

第 46~53 轮落地了 P4-B/P4-C 的完整链路与两项 P3 收尾，**全部未经执行验证**
（本会话 shell 从第 1 轮起不可用：`pwsh` 一律以 `0xC0000142` 退出）。

| 轮次 | 交付 | 测试 |
|---|---|---|
| 46 | 变更单状态机（9 态 + 迁移表）、下发/审批闸门、`change_requests`/`deploy_runs`、两个开关 | `tests/unit/test_change_flow.py`（27） |
| 47 | 数据库编排 `services/changes.py`、契约 `schemas/deploy.py`、10 条路由 | `tests/api/test_deploy_flow.py`（10） |
| 48 | 回滚内容计算 `rollback_lines()`：块内缺一行整块重发；删除类**一律不自动生成** | `tests/unit/test_rollback_lines.py`（11） |
| 49 | 一键回滚接上数据库与路由：候选按"创建它的那次采集"判定；无自动内容时 422 列出待人工处理的行 | `tests/api/test_rollback_flow.py`（5） |
| 50 | 看板聚合 `services/overview.py` + `/meta/overview`；冒烟名单纳入 P4 | `tests/api/test_overview.py`（2） |
| 51 | 跨设备检索 `/search/config`（auditor+、每次搜索写审计、limit 截断如实上报） | `tests/api/test_search.py`（3） |
| 52 | Git 正文后端 `GitBlobStore` + `build_blob_store()`：只有新对象才提交；git 失败不影响备份 | `tests/unit/test_git_blobs.py`（6） |
| 53 | 4 个 P4 开关同步进 `.env.example` / `docker-compose.yml` / 部署文档 | `test_deploy_config.py` 盯着 compose 透传 |

**验证入口**：`python scripts/smoke.py` 已把上述 58 条全部纳入（35 项 / 17 个整文件）。

**仍然做不了的**——每一块都缺一个**外部条件**，不是"再写点代码"能跨过去的：

| 剩余项 | 缺什么 |
|---|---|
| P4-A 真机下发（进卷积、逐行回显确认、`commit`） | 一台**可写的**真机，按厂商语法校准 |
| SNMP 自动发现 | SNMP 依赖 + 一台有 SNMP 的设备（或先写一个假 agent） |
| Celery 横向扩展 | Redis/broker + 多实例环境 |
| LDAP/AD/SSO | 目录服务器 |
| 前端合规/审计/风险变更三页 | 可验证的 `npm run build` 链路 |
| `risky_change` 事件 | 先清迁移欠账（给已有表加列会触发启动期漂移报错） |
| 冻结 `0001` 迁移基线 | 15 张表 + 约 25 个索引的机械重写，只能用迁移一致性测试验 |

**为什么不再继续往上堆未验证的代码**：§8.5.3 自己写着"P4 写的是生产设备，底座不能是
'大概没问题'"。在下发链路的底座（P1~P3 全量 + P4 已写的 58 条）拿到一次真实运行结论
之前，继续往这条"会改生产设备"的链上加代码，方向就是错的。

---

## 2. 总体架构

```
┌───────────────────────────────────────────────────────────────┐
│  浏览器 (Vue3 + Element Plus SPA)                              │
└───────────────────────┬───────────────────────────────────────┘
                        │ HTTPS / REST + SSE(实时日志)
┌───────────────────────▼───────────────────────────────────────┐
│  Nginx  ── 静态资源 / 反向代理 / TLS                           │
└───────────────────────┬───────────────────────────────────────┘
┌───────────────────────▼───────────────────────────────────────┐
│  FastAPI 应用层                                                │
│  ├─ API 路由层（devices / tasks / configs / diffs / rules）      │
│  ├─ 认证鉴权（JWT + RBAC 依赖注入）                             │
│  ├─ 业务服务层（DeviceService / BackupService / DiffService…）    │
│  └─ 实时通道（SSE：任务进度与设备输出流）                        │
└──────┬───────────────────────┬────────────────────────────────┘
       │                       │
┌──────▼──────────┐   ┌────────▼─────────────────────────────┐
│ 调度层           │   │  采集引擎 (asyncio)                   │
│ APScheduler      │   │  ├ 全局/分组 信号量并发控制            │
│ （cron/interval）│   │  ├ 设备级串行（同设备永不并发）         │
│                  │   │  ├ 重试 + 退避 + 抖动                 │
│                  │   │  ├ 连通性预检 (TCP:22/23)            │
└──────────────────┘   │  └ VendorDriver 插件注册表            │
                        └────┬───────────────────────────────┘
                             │ asyncssh / telnetlib3
                        ┌────▼────┐  ┌──────────┐  ┌──────────┐
                        │ 华为VRP │  │ 华三Comware│ │ 锐捷RGOS │
                        └─────────┘  └──────────┘  └──────────┘

┌───────────────────────────────────────────────────────────────┐
│  存储层                                                        │
│  ├ PostgreSQL / SQLite：设备台账、任务记录、快照元数据、审计日志  │
│  └ 文件系统 / MinIO / S3：配置正文（sha256 寻址，天然去重）        │
└───────────────────────────────────────────────────────────────┘
```

**核心设计原则**

1. **驱动与框架解耦**：加一个厂商 = 加一个 YAML/Python 驱动，不动采集引擎。
2. **快照不可变**：配置正文只写不改，用 sha256 做内容寻址；内容未变则只更新「已检查」时间戳，不产生新版本。
3. **失败可诊断**：每次采集落库一条 run 记录，保存原始会话日志（可脱敏后下载），排障不靠猜。
4. **凭证安全**：设备密码用 Fernet/KMS 加密存储，日志与 API 永不回显明文。

---

## 3. 技术选型

| 层次 | 选型 | 理由 / 备选 |
|---|---|---|
| 语言 | Python 3.11+ | 3.12/3.13 也可；注意 3.13 已移除 `telnetlib` |
| Web 框架 | **FastAPI** + Uvicorn | 原生 async，与 asyncio 采集引擎同进程模型；备选 Flask（同步，需线程池） |
| SSH 库 | **asyncssh**（主）/ **paramiko**（兜底） | asyncssh 高并发效率好；paramiko 对老旧 KEX/加密算法兼容性更宽容，老设备用 `Transport` 手工放开算法 |
| Telnet | **telnetlib3** / Exscript | 仅用于极老设备，默认关闭，需显式开启白名单 |
| ORM / 迁移 | SQLAlchemy 2.0(async) + Alembic | 成熟；备选 Tortoise-ORM |
| 数据库 | 开发 SQLite / 生产 **PostgreSQL 16** | 中小规模（<2000 设备）SQLite 亦可用 |
| 调度 | **APScheduler**（单机）/ **Celery + Redis**（多节点） | v1.0 单机 APScheduler 足够；横向扩展时换 Celery beat |
| 前端 | **Vue 3 + Vite + Element Plus + Pinia** | 表格/表单类运维后台最省力；团队若熟 React 则 Ant Design |
| 实时日志 | SSE（`text/event-stream`） | 比 WebSocket 简单，足够单向推送 |
| 加密 | cryptography (Fernet) / 可选 Vault | 主密钥来自环境变量或 KMS，不入库 |
| 日志 | structlog + JSON 输出 | 便于接入 ELK/Loki |
| 监控 | prometheus-client `/metrics` | 采集成功率、耗时、并发数 |
| 测试 | pytest + pytest-asyncio + respx + Playwright | 见第 6 节 mock 设备方案 |
| 部署 | Docker + docker-compose；离线 wheel 包 + `docker save` | 政企内网多为离线环境，必须交付离线安装包 |

---

## 4. 工程目录结构

```
dszoon/
├─ backend/
│  ├─ app/
│  │  ├─ main.py                 # FastAPI 入口、生命周期、路由挂载
│  │  ├─ core/
│  │  │  ├─ config.py            # pydantic-settings 配置
│  │  │  ├─ security.py          # JWT、密码哈希、RBAC 依赖
│  │  │  ├─ crypto.py            # 设备凭据加解密
│  │  │  └─ logging.py
│  │  ├─ models/                 # SQLAlchemy 模型
│  │  ├─ schemas/                # Pydantic DTO
│  │  ├─ api/v1/                 # devices/tasks/configs/diffs/rules/users/auth
│  │  ├─ services/               # 业务编排（备份、diff、基线、通知）
│  │  ├─ collector/              # ★ 采集引擎（核心资产）
│  │  │  ├─ session.py           # 会话抽象：读提示符、去 ANSI、处理 More、编码
│  │  │  ├─ engine.py            # 并发、重试、预检、run 记录
│  │  │  ├─ registry.py          # 驱动注册表
│  │  │  └─ drivers/
│  │  │     ├─ base.py           # VendorDriver 抽象基类
│  │  │     ├─ huawei.yaml       # 声明式驱动（命令/提示符正则/归一化规则）
│  │  │     ├─ h3c.yaml
│  │  │     ├─ ruijie.yaml
│  │  │     └─ custom_example.py # Python 逃生通道（特殊设备）
│  │  ├─ normalizer/             # 配置归一化 + 脱敏（供 diff / 合规复用）
│  │  ├─ rules/                  # 基线规则引擎
│  │  ├─ scheduler/              # APScheduler 作业注册
│  │  └─ storage/                # 本地/对象存储后端抽象
│  ├─ alembic/
│  ├─ tests/
│  │  ├─ fixtures/               # ★ 真实设备回显样本（脱敏）
│  │  │  ├─ huawei/vrp8/…
│  │  │  ├─ h3c/comware7/…
│  │  │  └─ ruijie/rgos11/…
│  │  ├─ mock_device/            # ★ 假设备 SSH/Telnet 服务器
│  │  ├─ unit/
│  │  └─ integration/
│  ├─ pyproject.toml
│  └─ Dockerfile
├─ frontend/
│  ├─ src/{api,views,components,stores,router}
│  └─ vite.config.ts
├─ deploy/
│  ├─ docker-compose.yml
│  ├─ nginx.conf
│  ├─ systemd/dszoon.service
│  └─ offline/                   # 离线安装脚本 + wheels
├─ docs/
│  ├─ DEVELOPMENT_PLAN.md
│  ├─ SPIKE_REPORT.md            # P0 阶段产出：各厂商实测坑位
│  ├─ DRIVER_GUIDE.md            # 如何新增一个厂商驱动
│  └─ API.md
└─ scripts/
```

---

## 5. 核心设计详述

### 5.1 设备驱动抽象

```python
# collector/drivers/base.py
class VendorDriver(ABC):
    name: str                      # "huawei" | "h3c" | "ruijie"
    display_name: str

    # 1) 提示符与登录态识别
    @abstractmethod
    def prompt_re(self, hostname: str) -> re.Pattern[str]: ...

    @abstractmethod
    def privilege_sequence(self, level: str) -> list[str]: ...   # 提权命令

    # 2) 会话预处理（关键：关分页 + 关换行，避免配置被折行破坏）
    @abstractmethod
    def session_setup_cmds(self) -> list[str]: ...

    # 3) 采集命令
    @abstractmethod
    def running_config_cmd(self) -> str: ...

    def startup_config_cmd(self) -> str | None: ...
    def version_cmd(self) -> str: ...

    # 4) 后处理
    def normalize(self, raw: str) -> str: ...        # 去易变行，供 diff
    def redact(self, raw: str) -> str: ...           # 口令/community 脱敏
    def extract_facts(self, version_out: str) -> dict: ...  # 型号/版本/序列号
```

驱动以 **YAML 声明式** 为主（覆盖 95% 场景），遇到怪设备再用 Python 子类覆盖某个方法。新增厂商无需改引擎代码。

### 5.2 会话层必须处理的 8 个坑（P0 阶段逐条实测）

| # | 问题 | 处理方式 |
|---|---|---|
| 1 | **分页** `---- More ----` | 登录后执行关分页命令；即使已关，读到 More 也补发空格并清理回显 |
| 2 | **折行截断**（配置行超终端宽度被折行 → 备份内容被破坏）| 登录后设置 `screen-width`/`terminal width` 到 512；这是最容易被忽略、后果最严重的问题 |
| 3 | **ANSI 转义序列**（如 `\x1b[42D`、`\b`） | 正则统一剥离，再按 `\r\n`/`\r` 归一 |
| 4 | **编码** | 优先 UTF-8；解码失败回退 GBK/GB18030（华三/锐捷含中文描述常见） |
| 5 | **登录横幅/首次登录提示** | 忽略 `Info:`、`Warning:`、改密提示，直到匹配到提示符 |
| 6 | **提权方式差异** | 华三 `super`+密码、锐捷 `enable`+密码、华为用户视图/系统视图；需在凭据模型里支持 `enable_password` |
| 7 | **老设备算法协商失败** | paramiko 兜底驱动：放开 `diffie-hellman-group1-sha1`、`ssh-rsa`、`aes128-cbc/3des-cbc`，并允许 `StrictHostKeyChecking=no`（记录指纹变更告警） |
| 8 | **会话卡死** | 双层超时：命令总超时 + 空闲超时；空闲超时触发一次「补发回车探活」，仍无提示符则失败并保留原始缓冲用于排障 |

### 5.3 各厂商命令对照（★ 需在 P0 阶段逐条实测校准）

| 用途 | 华为 VRP | 华三 Comware | 锐捷 RGOS |
|---|---|---|---|
| 关闭分页 | `screen-length 0 temporary` | `screen-length disable` | `terminal length 0` |
| 关闭折行 | `screen-width 512` | `screen-width 512` | `terminal width 512` |
| 当前配置 | `display current-configuration` | `display current-configuration` | `show running-config` |
| 下次启动配置 | `display saved-configuration` | `display saved-configuration` | `show startup-config` |
| 版本信息 | `display version` | `display version` | `show version` |
| 保存配置（可选，默认关） | `save`（需 Y 确认）/ 部分版本 `save force` | `save force` | `write` 或 `copy running-config startup-config` |
| 提示符 | `<HOST>` 用户视图 / `[HOST]` 系统视图 / `[HOST-xxx]` | `<HOST>` / `[HOST]` / `[HOST-if]` | `HOST#` / `HOST(config)#` |
| 提权 | 无需 enable，进系统视图 | `super` + 密码 | `enable` + 密码 |
| 典型编码 | UTF-8 | UTF-8 / GBK | UTF-8 / GBK |

> 上述命令与参数随 VRP5/VRP8、Comware 5/7/9、RGOS 10/11 差异较大，**必须先做 P0 实测**，产出 `docs/SPIKE_REPORT.md`，把「设备版本 → 命令/参数」做成驱动内的版本分支，而不是凭记忆写死。
>
> **已实测补充（华为 VRP 5.170 / S5720S，详见 `docs/SPIKE_REPORT.md`）**：
> 1. `screen-width 512` 会弹 `Warning: This command will change the default screen width. Continue? [Y/N]:` —— 必须自动回答，否则会话一直干等到超时（实测白等 65 秒，报错只是"会话空闲无数据"）。已实测**不会写进配置**，只影响本次会话。
> 2. `display version` 的型号行可能带产品描述（`HUAWEI S5720S-28X-SI-AC Routing Switch uptime is ...`），facts 正则必须兼容，否则型号静默丢失。
> 3. 真机配置里存在**约 100 字符的长行**，因此"关折行"不是可选项——不生效就会把长行劈成两半，而备份文件"看起来有内容"。
> 4. 采集器新增"预处理命令是否写进配置"的自动复查（`check_setup_leakage`），把"只读备份"从承诺变成可验证的事实。

### 5.4 并发与调度

- **设备级串行**：同一台设备任何时刻只允许 1 个采集会话（设备 VTY 数量有限，通常 5~16）。
- **全局并发**：默认 20，可配置；分组级限流（避免核心区被瞬间打满）。
- **错峰**：定时任务带 **0~N 分钟（只向后推迟，不提前）** 的随机抖动，避免整点雪崩。
  只向后是有意的：提前会让"02:00 开始"变成"01:55 就动了"，还可能落到执行窗口之外
  被跳过。**设备级的分散不靠抖动**——那是并发上限 + 同设备串行保证的。
- **预检**：先做 TCP 22/23 可达性探测，不可达直接标记 `unreachable`，不占用采集并发位。
- **重试**：连接类错误重试 2 次（指数退避 5s/15s）；认证失败不重试（避免锁账号）。
- **分批续采**：全网任务按批（如 50 台/批）提交，任务可暂停/取消，失败设备可单独重跑。

### 5.5 存储与版本模型

```
device            (id, hostname, ip, vendor, model, os_version, site, group_id, cred_profile_id, port, protocol, status, last_backup_at)
credential_profile(id, name, username, password_enc, enable_password_enc, key_ref)
backup_run        (id, device_id, trigger_type[manual|scheduled|retry], started_at, finished_at,
                   status[success|unchanged|auth_failed|unreachable|timeout|parse_error|error],
                   snapshot_id, bytes, duration_ms, error_class, error_msg, session_log_path)
config_snapshot   (id, device_id, sha256, storage_backend, object_key, size, captured_at, os_version, is_changed)
config_diff       (id, device_id, from_snapshot_id, to_snapshot_id, added, removed, changed, created_at)
baseline_rule / baseline_result / alert_channel / task_schedule / user / role / audit_log
```

存储策略：
- 配置正文走 **sha256 内容寻址**，同一内容多设备/多版本共享一份 blob，天然去重。
- **保留策略**：默认「保留全部变更版本 + 未变更版本按 90 天日备 + 首末版本永久」，可配置；清理任务只删 blob 无引用者。
- **diff 归一化**：先去掉易变噪声再比对，否则每天都会报「全量变更」：
  - `Last configuration` / `Current configuration : N bytes` / `Building configuration...`
  - 运行时长、时间戳、`ntp clock-period`、证书随机串、`!Software Version` 行单独抽取进元数据
  - 行尾空格统一、CRLF→LF
- **重要变更识别**：在 diff 之上叠加「关注清单」正则（ACL、interface shutdown、路由、AAA、SNMP community、NTP、日志服务器），命中即高优先级告警。

### 5.6 脱敏与加密

- 采集到的**原始配置**原样归档（审计需要），但：
  - Web 展示 / 导出 / 下载默认走 **redact 版本**（`password …`、`cipher`、`community`、`snmp-agent`、`key-string` 等按厂商正则掩码）。
  - 提供「查看明文」权限点，仅 admin/auditor 可用，且该动作写审计日志。
- 设备凭据用 Fernet 加密，主密钥来自环境变量/KMS；API 响应永不返回密码字段。
- 会话原始日志（用于排障）默认保存 7 天，保存前统一脱敏。

### 5.7 合规基线检查

YAML 规则，示例：

```yaml
- id: no-telnet-server
  vendors: [huawei, h3c, ruijie]
  desc: 禁止开启 Telnet 服务
  severity: high
  assert_absent: '^\s*telnet (server )?enable'
- id: ntp-configured
  vendors: [huawei, h3c, ruijie]
  desc: 必须配置 NTP
  severity: medium
  assert_present: '^\s*ntp-service unicast-server|^\s*ntp server'
- id: no-default-snmp-community
  severity: high
  assert_absent: 'community (public|private)\b'
```

引擎在每次采集成功后自动跑一遍，产出「设备 × 规则」矩阵报表，支持导出 Excel，并可按分组看合规率。

### 5.8 API 设计（主要接口）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/auth/login` | 登录，返回 JWT |
| GET/POST/PUT/DELETE | `/api/v1/devices` | 设备 CRUD（支持分页、按分组/厂商/状态筛选） |
| POST | `/api/v1/devices/import` | CSV/Excel 批量导入 + 预校验（重复 IP、格式、连通性） |
| GET | `/api/v1/devices/{id}/test` | 连接测试（只登录不采集） |
| POST | `/api/v1/backups` | 发起备份任务（设备集 / 分组 / 全网） |
| GET | `/api/v1/tasks/{id}` | 任务详情（进度、成功/失败数） |
| GET | `/api/v1/tasks/{id}/stream` | **SSE** 实时输出 |
| GET | `/api/v1/devices/{id}/snapshots` | 版本列表 |
| GET | `/api/v1/snapshots/{id}/content` | 配置正文（`?format=raw\|redacted`，`?download=1`） |
| GET | `/api/v1/diffs?from=&to=` | 两版本差异（unified diff + 结构化变更块） |
| GET/POST | `/api/v1/schedules` | 定时策略 CRUD |
| GET | `/api/v1/compliance/report` | 合规报表 |
| GET | `/api/v1/audit-logs` | 审计日志 |
| GET | `/api/v1/metrics` | Prometheus 指标 |

### 5.9 权限与审计

| 角色 | 权限 |
|---|---|
| admin | 全部，含凭据管理、用户管理、查看明文 |
| operator | 设备/任务/调度管理，可备份，不可管理凭据与用户 |
| auditor | 只读 + 查看明文 + 审计日志导出 |
| viewer | 只读（脱敏配置） |

审计事件：登录/登出、凭据增删改、设备增删改、发起备份、下载配置、查看明文、变更调度、修改规则。

### 5.10 通知与可观测性

- 通知渠道：邮件(SMTP)、Webhook、钉钉/企业微信/飞书机器人。
- 触发条件：任务整体失败率 > 阈值、单设备连续 N 次失败、发现「重要变更」、基线不合规项新增。
- 指标：`backup_total{status}`、`backup_duration_seconds`、`device_unreachable_total`、`collector_queue_depth`。
- 健康检查：`/healthz`（进程）、`/readyz`（DB + 存储可用）。

---

## 6. 测试策略（决定项目能走多远）

1. **录制回放夹具**：从真实设备采集回显（脱敏后）存成 `fixtures/<vendor>/<os>/<cmd>.txt`，单元测试直接断言「原始回显 → 清洗后配置」的结果。**这是最重要的一项资产**，覆盖分页残留、ANSI、GBK、折行等所有历史坑。
2. **Mock 设备服务器**（`tests/mock_device/`）：用 asyncssh 起一个假 SSH 服务器，按 YAML 剧本回放交互。
   - 剧本可模拟：正常回显、分页 `---- More ----`、超长输出、中途断连、认证失败、改密提示、老算法协商。
   - 好处：CI 里不需要 eNSP/HCL/EVE-NG，测试秒级完成且完全确定。
3. **虚拟真机联调**（可选，发布前）：eNSP（华为）、HCL（华三）、EVE-NG/GNS3 + 镜像，做一轮端到端验证。
4. **契约测试**：新增厂商驱动必须附带夹具 + 单测，`DRIVER_GUIDE.md` 里作为硬性提交要求。
5. **前端 E2E**：Playwright 覆盖登录 → 建设备 → 备份 → 看 diff → 导出。
6. **性能压测**：mock 设备开 500 个实例，验证 20 并发下的成功率与内存占用。

---

## 7. 部署与交付形态

| 形态 | 说明 |
|---|---|
| Docker Compose（推荐） | `nginx + app + postgres`，一条 `docker compose up -d` |
| 离线包 | 内网无外网：交付 `wheels/` + `docker save` 的镜像 tar + 一键安装脚本 |
| 裸机 systemd | `dszoon.service`，适合只有一台 Linux 虚机的场景 |
| Windows Server | 纯 Python 依赖（asyncssh/paramiko 无 C 扩展硬依赖）+ WinSW/NSSM 注册服务；或 Docker Desktop |

网络要求：运行主机到设备管理网 22/23 端口可达；若必须经跳板机，需支持 **SSH 跳板（ProxyJump）**（README 中明确，v1.0 支持）。

---

## 8. 里程碑与排期（1~2 人，按周）

| 阶段 | 周期 | 交付内容 | 验收标准 |
|---|---|---|---|
| **P0 技术验证** | 1 周 | 三家厂商各 2~3 个真实版本的手工采集脚本；`SPIKE_REPORT.md`；fixtures 库雏形；与假设备联调通过 | 三家设备均能采到**完整未被折行破坏**的配置；明确记录每个版本的正确关分页/关折行命令与提权方式 |
| **P1 MVP** | 2~3 周 | 驱动框架 + 会话层 + 三大驱动；SQLite + 文件存储；设备 CRUD；单设备/批量手动备份；配置查看/下载；近两版 diff；登录鉴权 | 内网 50 台真实设备批量备份成功率 ≥ 95%；采到的配置与人工 `display current-configuration` 逐字节比对一致（除噪声行） |
| **P2 规模化** | 2~3 周 | APScheduler 定时策略；并发/限流/重试/预检；任务实时日志(SSE)；凭据加密；分组标签；CSV 批量导入；保留策略与清理；通知渠道 | 1000 台规模定时任务稳定运行 1 周无漏备；单台失败不影响整体；失败原因可定位到会话日志 |
| **P3 运维增强** | 2 周 | 合规基线引擎 + 报表导出；重要变更识别与告警；RBAC + 审计日志；全文检索（跨设备搜配置）；Vue 前端完整化；Prometheus 指标 | 满足等保审计要求：谁在何时下载了哪台设备的配置可追溯 |
| **P4 高级特性（可选）** | 2~4 周 | 配置下发/回滚（审批流 + diff 预览 + 二次确认）；SNMP 自动发现；Git 后端存储；Celery 多节点横向扩展；LDAP/AD/SSO | 回滚特性通过灰度验证，具备「一键回滚到上一个可用版本」能力 |

**总工期**：MVP 约 3~4 周；v1.0（P0~P3）约 8~10 周；含 P4 约 12~14 周。

> 排期前提：P0 取得真实设备/虚拟设备访问权限。若拿不到真机，P0 需延长并依赖 mock 设备 + 社区样本，风险显著上升（见第 9 节 R1）。

---

## 9. 风险登记表

| 编号 | 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|---|
| R1 | **拿不到真实设备/版本覆盖不足**，命令与提示符凭猜测 | 中 | 高 | P0 必须拿到至少华为/H3C/锐捷各 2 个版本的访问权限；建 fixtures 库；驱动做成版本分支 + 可被用户配置覆盖 |
| R2 | 老设备算法不被 asyncssh 支持 | 高 | 中 | paramiko 兜底驱动；算法白名单可配置；驱动级降级策略 |
| R3 | 配置被折行/分页截断，备份内容不可用 | 中 | **极高** | P0 专项验证；采集后做完整性校验（首行/末行特征 + 括号/`#` 结构配平 + 断言以 `return`/`end` 结尾），不通过即标记异常 |
| R4 | 编码错误导致中文描述乱码或截断 | 中 | 中 | 多编码探测回退；夹具覆盖 GBK 场景 |
| R5 | 并发打满设备 VTY，影响生产网运维 | 中 | 高 | 设备级串行 + 全局/分组限流 + 错峰抖动 + 预检；默认保守值 |
| R6 | 凭据泄露 | 低 | 极高 | 加密存储、最小权限、明文查看审计、会话日志脱敏、支持凭据轮换 |
| R7 | 设备密码策略变更导致大面积认证失败 | 中 | 中 | 认证失败不重试、批量失败自动熔断并告警、支持凭据组批量更新 |
| R8 | 内网离线安装踩依赖坑 | 中 | 中 | 第 7 节的离线包方案，发布前在隔离环境演练安装 |
| R9 | 范围蔓延到「配置下发」引发生产事故 | 中 | 高 | v1.0 明确不做；P4 引入时强制审批流 + diff 预览 + 双人确认 |

---

## 10. 完成的定义（DoD）

一个功能算「完成」，必须同时满足：

- [ ] 有单元测试，且新增厂商/新坑位补充了 fixtures
- [ ] 通过 mock 设备集成测试
- [ ] 关键路径有结构化日志与指标
- [ ] 异常路径（不可达/认证失败/超时/中断）有明确状态码与错误文案，不是裸 `Exception`
- [ ] 文档更新（`API.md` 或 `DRIVER_GUIDE.md`）
- [ ] 涉及敏感数据的功能通过了脱敏自查

---

## 11. 第一周（P0）具体任务清单

1. **环境**：Python 3.11 venv、`pyproject.toml`、pytest、pre-commit、ruff+mypy。
2. **会话原型**（`collector/session.py` 雏形）：
   - asyncssh 连接 → 执行 setup 命令 → 读提示符 → 抓取配置 → 剥离 ANSI/回显 → 落盘。
   - 打印每一步耗时，定位慢点（华为大配置可达 20~60s）。
3. **三家设备实测**，每台记录：型号、软件版本、正确关分页命令、正确关折行命令、提权方式、编码、单次采集耗时、典型坑。
   - 华为：`screen-length 0 temporary` + `screen-width 512`
   - 华三：`screen-length disable` + `screen-width 512`
   - 锐捷：`terminal length 0` + `terminal width 512`
4. **完整性校验函数**：断言采集结果以设备配置结尾标志（如 `return` / `end`）闭合，且无 `---- More ----` 残留、无异常短行。
5. **夹具沉淀**：把原始回显（脱敏）存入 `tests/fixtures/`，并为清洗逻辑写第一批单测。
6. **Mock 设备**：asyncssh 假服务器跑通一次脚本化回放单测。
7. **产出 `docs/SPIKE_REPORT.md`** → 用它反向校准第 5.3 节命令表，再正式开工 P1。

---

## 12. 待确认问题（影响架构与排期）

1. **设备规模与型号**：多少台？华为/华三/锐捷各占多少？主要软件版本（VRP5/8、Comware5/7/9、RGOS10/11）？
2. **网络可达性**：采集服务器能否直连设备管理网？是否需要 SSH 跳板机？
3. **是否需要配置下发/回滚**？如需，是 v1.0 就要还是放到 P4（风险与工期差异很大）。
4. **部署环境**：Linux 还是 Windows？是否内外网隔离（决定离线包工作量）？数据库偏好 PostgreSQL 还是 SQLite？
5. **用户与认证**：多少用户？是否需要对接 LDAP/AD/统一认证？
6. **合规要求**：是否有等保/审计留痕要求？是否需要基线合规检查。
7. **前端偏好**：Vue 还是 React？团队现有技能栈。
