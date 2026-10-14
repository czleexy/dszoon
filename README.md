# dszoon — 网络设备配置备份系统

面向华为 VRP、华三 Comware、锐捷 RGOS 的网络设备配置备份与变更管控平台。
定时把设备配置取回来、存档、比对、核对合规基线；配置一变就能看到**改了什么、风险多高**。

**对设备只读**：只登录、只执行查看命令，不下发任何配置。（配置下发/回滚是 P4 的在研项，
接口已预留 `VendorDriver.deploy()`，v1.0 一律抛 `DeployNotSupported`。）

## 它解决的是什么问题

现网常见的做法是人工 telnet 上去 `display current-configuration`、复制粘贴到一个文件夹里，
于是：改了没人知道、改错没人拦、出事找不到"上一版好在哪"、几百台设备没人管得过来。

这个项目把这些变成可审计的流程：**采集是自动的、版本是内容寻址的、变更是可解释的、
合规是可复核的、操作是留痕的**。

## 功能

| 能力 | 说明 |
|---|---|
| 配置采集 | asyncssh 主用 + paramiko 兜底；提示符驱动的读取循环；ANSI/退格/分页/编码清洗；完整性校验（结束标记、最小行数、错误特征、折行告警） |
| 版本存储 | 配置正文内容寻址落盘（同内容只存一份，天然去重），数据库只存指针与元数据 |
| 设备台账 | 分组、站点、凭据档案（Fernet 加密，接口永不返回密文/明文）、批量导入、连通性预检 |
| 变更比对 | 逐行 diff + 风险分级（10 类变更模式，含删除侧），"重要变更"可解释 |
| 定时调度 | 数据库即唯一事实来源的调度器（无内存 job 同步问题）；cron 表达式、下次执行预览、手工触发不影响计划 |
| 保留策略 | 按天数/版本数保守清理，正文回收留宽限期，避免删掉正在写入的版本 |
| 合规基线 | YAML 规则（内置 16 条：8 high / 7 medium / 1 low），违规给脱敏片段与整改建议，可整体替换为自有基线 |
| 审计 | 敏感读取、导出、下发类操作全部留痕；审计日志可查询、可导出（导出本身也被审计） |
| 通知 | Webhook / 钉钉 / 企业微信 / 飞书 / SMTP；事件含采集失败、设备连续失败、合规未通过 |
| 监控 | Prometheus `/api/v1/metrics`，标签基数有界；`/healthz` `/readyz` |
| 部署 | Docker Compose / systemd / 离线内网包 / Windows Server 四种形态 |

## 技术栈

- **后端**：Python 3.11+、FastAPI、SQLAlchemy 2.x（async）、Pydantic v2、PyJWT、cryptography（Fernet）、APScheduler 3.x、httpx
- **存储**：SQLite（单机默认）或 PostgreSQL 16（生产）
- **前端**：Vue 3 + Vite + Element Plus（构建产物由后端直接挂载，同源部署）
- **采集**：asyncssh（主）+ paramiko（老旧设备算法兜底）

## 快速开始（开发机）

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate          # Windows；Linux/macOS: source .venv/bin/activate
pip install -e ".[dev,web]"

# ⓿ 首跑前的 10 秒体检（只读：导入链 / 依赖 / 旧库 / 前端产物 / 前端导入一致性）
python scripts/preflight.py

# ① 分层自检：环境 → 语法 → 导入 → 清洗 → 建库 → 端到端备份 → 运营链路
python scripts/selfcheck.py
# ② 冒烟子集：每条压一个子系统，失败能立刻定位范围
python scripts/smoke.py
# ③ 完整回归（★ 用 python -m pytest，别用裸 pytest：可能不是同一个解释器）
python -m pytest -q

# 起服务
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

前端：

```bash
cd backend/web
npm install
npm run build     # 产物 web/dist，由后端自动挂载
npm run dev       # 开发模式（5173，已在 CORS 白名单里）
```

默认用 SQLite（`backend/var/dszoon.db`），初始管理员 `admin`。
口令取 `DSZOON_ADMIN_PASSWORD`，未设置时随机生成并在**首次建库那一次**的启动日志里打印一次。

> 沿用旧版本的开发库会启动失败并提示 `数据库 schema 比代码旧：缺少 …`——
> 这是有意的：`create_all` 只补建缺失的表、不会给已有表加列。删掉 `var/dszoon.db`
> 重建即可（**保留 `var/.secrets.json`**，删了它设备密码就解不开了）。

## 部署

**完整部署文档见 [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)**：四种形态的逐步操作、
环境变量总表、端口与网络要求、备份与恢复演练、升级流程、故障处置表、安全清单。

| 场景 | 入口 |
|---|---|
| Docker Compose（推荐） | `backend/deploy/docker-compose.yml` + `backend/deploy/.env.example` |
| systemd 单机 | `backend/deploy/systemd/dszoon.service` |
| 离线内网（无外网） | `backend/deploy/offline/README.md` |
| Windows Server | `docs/DEPLOYMENT.md` §6 |

## 文档

| 文档 | 内容 |
|---|---|
| [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md) | 总计划、阶段划分、里程碑、P4 安全约束 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 部署与运维（交付/实施看这份） |
| [`docs/P1_RUNBOOK.md`](docs/P1_RUNBOOK.md) | 开发机验证流程、失败特征对照表 |
| [`docs/P2_ACCEPTANCE.md`](docs/P2_ACCEPTANCE.md) / [`docs/P3_ACCEPTANCE.md`](docs/P3_ACCEPTANCE.md) | 阶段验收步骤与预期结果 |
| [`backend/README.md`](backend/README.md) | 代码结构与模块说明 |
| [`docs/SPIKE_REPORT.md`](docs/SPIKE_REPORT.md) | 采集层真机实测报告 |

## 仓库结构

```
DEVELOPMENT_PLAN.md    总计划与进度
docs/                  部署文档、各阶段验收、实测报告
backend/
  app/                 后端（core / db / schemas / services / api / collector / compliance）
  tests/               单元 + 接口 + 集成测试，含真实回显夹具与假设备
  web/                 Vue 3 前端
  deploy/              Dockerfile / compose / nginx / systemd / 离线打包脚本
  migrations/          Alembic 迁移
  scripts/             preflight / selfcheck / smoke / loadtest / 真机探针
```

## 安全

- 设备凭据用 Fernet 加密存储，任何接口都不返回密码（连密文都不返回）
- `.env`、`var/.secrets.json`、`var/`（配置正文与会话日志）、`*.db` 均在 `.gitignore` 中，不进版本库
- 采集账号**只需只读权限**，设备侧不要授予配置权限
- `/api/v1/metrics`、`/healthz`、`/readyz`、`/docs` 不要求登录，生产环境请用反向代理限制来源网段

## 状态与许可

开发中：P0（采集层）已完成并真机验证，P1/P2/P3 已实现，
P4（配置下发与回滚、SNMP 自动发现、Git 存储后端、Celery 分布式执行）进行中。
各阶段范围见 [`DEVELOPMENT_PLAN.md`](DEVELOPMENT_PLAN.md)。

本仓库**尚未附带开源许可证**，即默认保留所有权利。如需对外分发或开源，
请先明确许可证（内部项目通常选 `Proprietary`，开源可考虑 Apache-2.0 / MIT）。
