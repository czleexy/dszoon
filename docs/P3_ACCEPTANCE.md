# P3 验收与验证 Runbook（运维增强）

> **为什么有这份文档**：agent 的执行环境里 shell 始终不可用（进程启动即 `0xC0000142`），
> 所以 P3 的代码与约 60 个测试**全部只经过静态审查，从未运行过**。验证只能由人在能跑的
> 机器上完成。下面把"跑什么、期望看到什么、红了先看哪里"一次写清楚。

---

## 1. P3 交付物与对应文件

| 交付项 | 主要文件 |
|---|---|
| 合规规则层（YAML → 数据、加载即编译、id 唯一、版本指纹） | `app/compliance/rules.py` |
| 合规评估引擎（纯函数：present/absent、severity→verdict、违规行脱敏） | `app/compliance/engine.py` |
| 内置基线 16 条（三家厂商语法差异，8 条 high） | `app/compliance/baselines/baseline.yaml` |
| 结果表 + 采集后自动评估 | `compliance_results`（`app/db/models.py`）、`app/services/backups.py::_check_compliance` |
| 合规服务（矩阵报表、合规率、CSV 拍平、最新结果窗口查询） | `app/services/compliance.py` |
| 合规接口 7 个 | `app/api/v1/compliance.py`、`app/schemas/compliance.py` |
| 不合规通知事件 `compliance_failed` | `app/services/notifications.py` |
| 审计查询/动作字典/下载记录视图/导出（导出本身留痕） | `app/api/v1/audit.py`、`app/schemas/audit.py` |
| 重要变更分级识别（10 类关注清单，含删除侧） | `app/services/diffs.py::classify_change` |
| Prometheus 指标 + 端点 | `app/services/metrics.py`、`app/api/v1/metrics.py` |
| 测试 | `tests/unit/test_compliance.py`(21)、`tests/unit/test_diffs_risk.py`(10)、`tests/unit/test_metrics.py`(6)、`tests/api/test_compliance.py`(6)、`tests/api/test_audit.py`(5) |

---

## 2. 执行

```powershell
cd F:\dszoon\backend
.venv\Scripts\activate
python scripts/preflight.py    # ⓿ 只读体检（导入链/依赖/旧库/前端产物）
python scripts/smoke.py        # P2 修复 + P3 接口层，26 项 / 8 个整文件 / 3~4 分钟
python -m pytest -q            # 全量（约 7 分钟）
```

> ⚠ 冒烟里最后两项（`test_compliance.py` / `test_audit.py`）要真跑备份，**最慢但最有信息量**：
> 它们会同时验证"合规评估挂在备份收尾路径上"和"审计能回答谁下载了配置"。

---

## 3. 期望结果（关键项）

| 检查 | 期望 |
|---|---|
| `python scripts/smoke.py` | 0 failed。合规/分级识别/指标三组纯逻辑是秒级的 |
| `python -m pytest -q` | 0 failed；1 skipped 属预期（SQLite 不允许删被外键引用的列） |
| `GET /api/v1/compliance/rules` | 16 条规则 + 非空 `version` 指纹 |
| `GET /api/v1/compliance/report` | `rule_columns` ≥15；**没有采集过的设备也在 matrix 里**（`verdict: unknown`） |
| 华为夹具设备 | 判定 `warn`（只缺空闲超时） |
| 华三夹具设备 | 判定 `fail`（**它确实开着 telnet**） |
| 锐捷夹具设备 | 判定 `fail`（**public/private 默认团体名 + type-7 口令**） |
| `GET /api/v1/audit-logs/downloads` | 下载/看明文之后能查到 **谁/何时/哪台设备** 三要素 |
| `GET /api/v1/audit-logs/export?fmt=csv` | 带 UTF-8 BOM（Excel 不乱码），且产生一条 `audit.export` |
| `GET /api/v1/metrics` | exposition 文本；`dszoon_up 1`；`dszoon_schedules_due_total` 正常应为 0 |

---

## 4. 失败特征对照表（P3 新增）

| 现象 | 原因 / 处理 |
|---|---|
| 规则文件报 `RuleError: ... 无法编译` | 正则写错——**这是有意的**（加载即报错，不静默跳过） |
| 合规报表全 `unknown` | 采集后没评估：查 `DSZOON_COMPLIANCE_ENABLED`（默认 true）、查后台日志里"合规基线评估失败" |
| 合规判定与预期不符 | 先看 `tests/unit/test_compliance.py` 里三份夹具的预期；改规则必须同步改那里的断言 |
| `compliance_failed` 从不触发 | 判定用的是"**本次任务产出的 snapshot** + `checked_at >= task.created_at`"。若改成只按时间（例如 `task.started_at`）会漏报：任务时间戳晚于落盘时刻时条件永不成立（第一次运行就踩过） |
| `/metrics` 返回 404 | 路由没注册（`api/v1/router.py` 是否 include 了 `metrics.router`） |
| Prometheus 报 invalid format | 指标名/标签值不合规——`tests/unit/test_metrics.py` 会先报出来 |
| 审计接口 403 | 读审计要 auditor 及以上。**注意角色是包含式的**（admin > operator > auditor > viewer），所以 operator 也能读——被挡住的只有 viewer 与未登录。要把运维挡在审计之外，得引入独立权限点，不能靠这个层级 |
| CSV 中文乱码 | 必须 `utf-8-sig`（带 BOM）；测试里已断言响应体以 `EF BB BF` 开头 |
| 启动报"数据库 schema 比代码旧" | P3 没加列，但 P2 加过；开发环境删 `var/dszoon.db` 重启（**保留 `.secrets.json`**） |

---

## 5. 仍未做（P3 剩余项，别误以为已交付）

1. `risky_change` 通知事件 —— 需给 `backup_runs` 加 `risk_level` / `risk_hits` 两列
   （分级识别本身已完成并有单测，只是还没接到采集落盘与告警上）
2. 前端三个页面：合规报表、审计查询、风险变更列表
3. 跨设备全文检索（需建索引表；直接扫 blob 不可行）
4. 看板上的合规率与风险变更聚合
