# dszoon 部署文档

面向交付与运维。所有配置项、端口、命令都对照 `backend/deploy/` 与 `backend/app/core/config.py` 写成，  
**不是示意**——命令可以直接复制执行。

配套文档：

| 文档                                                | 用途               |
| ------------------------------------------------- | ---------------- |
| `docs/P1_RUNBOOK.md`                              | 开发机验证：自检、回归、真机联调 |
| `docs/P2_ACCEPTANCE.md` / `docs/P3_ACCEPTANCE.md` | 各阶段验收步骤与预期结果     |
| `DEVELOPMENT_PLAN.md`                             | 开发计划与里程碑         |
| `backend/deploy/offline/README.md`                | 离线包细节            |

---

## 0. 先看这一节：当前版本能做什么、不能做什么

**v0.1.0 对设备是只读的。** 它只登录设备、执行只读查看命令、把配置取回来存库比对，  
**不会下发任何配置、不会改设备**。`VendorDriver.deploy()` 是预留接口，配置下发/回滚/审批  
（P4）仍在开发中，尚未接入任何 API。

按现在的代码，部署后你得到：

- 设备台账（含凭据加密存储）、分组、连通性预检
- 定时/手工采集配置，版本比对（含风险分级）、保留策略自动清理
- 合规基线检查（内置 16 条规则，8 条 high）、审计日志、通知渠道、Prometheus 指标

尚未包含（不要按"应该有了"验收）：Telnet 采集、SNMP 自动发现、LDAP/AD/SSO 登录、  
Git 存储后端、Celery 分布式执行、配置下发与回滚。这些是 P4 的剩余项。

---

## 1. 选哪种部署形态

| 形态                    | 适用场景                        | 数据库                 | 复杂度 | 见  |
| --------------------- | --------------------------- | ------------------- | --- | -- |
| **A. Docker Compose** | 有 Docker 的 Linux 服务器，**推荐** | PostgreSQL 16       | 低   | §3 |
| B. systemd 单机         | 不能用 Docker、要贴 OS 加固基线       | PostgreSQL 或 SQLite | 中   | §4 |
| C. 离线内网包              | 政企内网，无外网                    | SQLite / 内网 PG      | 中   | §5 |
| D. Windows Server     | 现场只有 Windows                | SQLite / PG         | 中   | §6 |
| E. 本地试用               | 开发机先跑通看界面                   | SQLite              | 最低  | §7 |

选型只有两条判断：**设备台数 > 50 或要多人同时用 → PostgreSQL**；否则单机 SQLite 也够，  
但要把数据目录放进备份。

---

## 2. 部署前必须定下的四件事

这四件事定错了，后面每个操作都会踩坑，所以放在最前面。

### 2.1 表的归属：应用自动建，还是 Alembic 迁移

`DSZOON_AUTO_CREATE_SCHEMA` 决定谁来建表，**两者只能选一个**：

| 值          | 行为                       | 用在哪          |
| ---------- | ------------------------ | ------------ |
| `true`（默认） | 启动时 `create_all()` 建缺失的表 | 开发、单机、临时试点   |
| `false`    | 启动时只检查，缺表直接报错并给出命令       | **正式环境（推荐）** |

必须在部署时定下来，理由：`create_all()` **只补建缺失的表，不会给已有表加列**。  
两种方式混用时，迁移历史会和真实 schema 悄悄分叉，等到某天报"表已存在"或  
"列不存在"时，几乎查不出是哪一步跑偏的。

正式环境的正确顺序（两步，顺序不能反）：

```bash
# 1) 空库上先跑迁移建表
docker compose exec app alembic upgrade head
# 2) 再把 DSZOON_AUTO_CREATE_SCHEMA 改成 false 并重启
```

已经是 `true` 跑了一段时间的库，见 §12.2。

### 2.2 两个密钥：生成后必须长期保存

| 变量                  | 作用             | 换了会怎样               |
| ------------------- | -------------- | ------------------- |
| `DSZOON_SECRET_KEY` | 设备凭据加密（Fernet） | **所有设备密码解不开，要逐台重录** |
| `DSZOON_JWT_SECRET` | 登录令牌签名         | 所有人被强制重新登录（可接受）     |

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

留空时应用会自动生成并写到 `$DSZOON_DATA_DIR/.secrets.json`（权限 0600）并在日志里告警。  
**自动生成只适合开发**：容器重建、数据卷丢失都会导致密钥消失，设备密码随即报废。

### 2.3 数据目录：丢了它 = 丢了所有历史配置

`DSZOON_DATA_DIR`（默认 `var`）里放两样东西：

| 子目录         | 内容                                              | 说明           |
| ----------- | ----------------------------------------------- | ------------ |
| `objects/`  | 配置正文（内容寻址，按 sha256 分片存放）                        | **数据库里只有指针** |
| `sessions/` | 原始会话日志（排障用，`DSZOON_KEEP_SESSION_LOGS=false` 可关） |              |

必须放在**持久化磁盘**上，且纳入备份。数据库和它必须**成对**恢复（§11）。

### 2.4 数据库地址

| 场景         | `DSZOON_DATABASE_URL`                           |
| ---------- | ----------------------------------------------- |
| 单机 SQLite  | `sqlite+aiosqlite:///./var/dszoon.db`（默认）       |
| PostgreSQL | `postgresql+asyncpg://dszoon:口令@db:5432/dszoon` |

`asyncpg` 已含在 `[web]` extras 里，不需要额外装。

---

## 3. 形态 A：Docker Compose（推荐）

文件：`backend/deploy/docker-compose.yml`、`backend/deploy/Dockerfile`、`backend/deploy/.env.example`。

镜像内容说明：前端在构建阶段用 node:20 编译（产物挂到 `/`，不需要单独跑前端服务），  
运行镜像基于 `python:3.12-slim`，**以 uid 10001 非 root 运行**，数据卷挂 `/data`，  
自带 `/healthz` 健康检查。

### 3.1 准备 .env

```bash
cd backend/deploy
cp .env.example .env      # Windows: copy .env.example .env
```

必须填的（compose 用了 `:?`，不填直接启动失败）：

```ini
POSTGRES_PASSWORD=<强口令>
DSZOON_SECRET_KEY=<上面生成的 Fernet key>
DSZOON_JWT_SECRET=<上面生成的随机串>
```

建议同时填：

```ini
DSZOON_ADMIN_PASSWORD=<初始管理员口令>   # 留空则随机生成并在启动日志打印一次
TZ=Asia/Shanghai                        # ★ cron 的"每天 02:00"按容器本地时间算
```

### 3.2 起服务

```bash
docker compose up -d --build
docker compose logs -f app        # 看初始化日志（管理员口令、已挂载的前端、密钥告警）
```

访问 `http://<服务器IP>:8000`。

> `.env` 里的其它开关**不会自动进容器**：compose 用的是显式列环境变量（不是 `env_file`），  
> 每加一项都要在 `docker-compose.yml` 的 `app.environment` 里加一行。漏掉的后果是  
> "文档写了、代码支持了、容器收不到"，而且在单机开发环境永远暴露不出来。  
> `tests/unit/test_deploy_config.py` 会盯着这一点。

### 3.3 加 nginx（HTTPS / 反向代理）

```bash
# 证书放 backend/deploy/certs/（fullchain.pem + privkey.pem），
# 并在 nginx.conf 里打开 listen 443 ssl 那三行注释
docker compose --profile with-nginx up -d
```

nginx 在 `with-nginx` profile 里，不加 `--profile` 不会启动。  
启用 nginx 后**建议把 compose 里 `ports: ${APP_PORT:-8000}:8000` 那行注释掉**，  
否则 8000 端口绕过 TLS 直连，等于 HTTPS 白配。

`nginx.conf` 里已经处理了两个必须处理的点，改配置时不要删：

- **SSE 不缓冲**（`proxy_buffering off`）：`/api/v1/backups/<id>/stream` 是任务实时进度，  
  缓冲开着会让前端等任务结束才一次性收到全部事件，看起来像"进度条不动"；
- **读超时放大**（`proxy_read_timeout 900s`）：大配置采集可能几十秒。

### 3.4 验收

```bash
curl -s http://127.0.0.1:8000/healthz     # {"status":"ok","version":"0.1.0"}
curl -s http://127.0.0.1:8000/readyz      # database=true；DB 不可达时 503
docker compose exec app python scripts/selfcheck.py   # 端到端自检
```

`selfcheck.py` 能在容器里跑，是因为 Dockerfile 特意把 `scripts/` 与 `tests/` 打进了镜像。  
注意镜像只装了 `[web]` extras，**容器里没有 pytest**；要跑 `pytest` / `scripts/smoke.py`  
得在源码环境里跑。

---

## 4. 形态 B：systemd 单机（不用 Docker）

模板：`backend/deploy/systemd/dszoon.service`（文件末尾有安装步骤）。

```bash
# 1) 用户与目录
useradd -r -s /sbin/nologin dszoon
install -d -o dszoon -g dszoon /opt/dszoon

# 2) 代码放 /opt/dszoon/backend，虚拟环境放 /opt/dszoon/venv
cd /opt/dszoon/backend
python3 -m venv /opt/dszoon/venv
/opt/dszoon/venv/bin/pip install ".[web]"

# 3) 前端产物（服务从 /opt/dszoon/backend 启动，会挂载 web/dist）
cd web && npm install --no-audit --no-fund && npm run build

# 4) 密钥等敏感配置：放文件，不写命令行（ps 能看到）
cat > /etc/dszoon.env <<'EOF'
DSZOON_DATABASE_URL=postgresql+asyncpg://dszoon:口令@127.0.0.1:5432/dszoon
DSZOON_DATA_DIR=/opt/dszoon/backend/var
DSZOON_SECRET_KEY=<Fernet key>
DSZOON_JWT_SECRET=<随机串>
DSZOON_AUTO_CREATE_SCHEMA=false
TZ=Asia/Shanghai
EOF
chmod 600 /etc/dszoon.env && chown dszoon /etc/dszoon.env

# 5) 注册服务
cp /opt/dszoon/backend/deploy/systemd/dszoon.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now dszoon
journalctl -u dszoon -f
```

三个容易踩的点：

1. **`ReadWritePaths` 必须包含数据目录**。unit 里写死了 `/opt/dszoon/backend/var`  
   （`ProtectSystem=full` 会让其它路径只读）。改了 `DSZOON_DATA_DIR` 就要同步改这一行，  
   否则表现为"启动正常，一备份就写文件失败"。
2. **从 `backend/` 目录启动**。`main.py` 按 `web/dist`、`../frontend/dist`、  
   `frontend/dist` 依次找前端产物，都是相对当前工作目录；`WorkingDirectory` 写错会  
   得到"只有 API、界面 404"。启动日志会打印实际挂载的目录，先看那一行。
3. systemd 的 `EnvironmentFile` 会**透传全部变量**（和 compose 不同），  
   `.env.example` 里每个开关都能直接写进 `/etc/dszoon.env`。

---

## 5. 形态 C：离线内网包

详细步骤见 `backend/deploy/offline/README.md`，这里只列要点。

联网机器上打包：

```bash
cd backend
bash deploy/offline/make_bundle.sh
# 产物：deploy/offline/dist/dszoon-offline-<日期>.tar.gz
```

目标机器上：

```bash
tar -xzf dszoon-offline-<日期>.tar.gz && cd dszoon-offline-<日期>
bash install.sh      # 建 venv、离线装轮子、放前端产物、生成带随机密钥的 .env
./venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

**目标机的 Python 小版本要和打包机一致**，否则可能下到不匹配的 wheel。  
打包脚本已经额外带了 `setuptools`/`wheel`：Python 3.12 起 venv 不再自带 setuptools，  
离线机 `pip install -e . --no-index` 会直接失败。

纯 Docker 内网交付（不装 Python）：

```bash
# 联网机器
docker build -f deploy/Dockerfile -t dszoon:0.1.0 .          # 在 backend/ 下执行
docker save dszoon:0.1.0 | gzip > dszoon-image.tar.gz
docker pull postgres:16-alpine && docker save postgres:16-alpine | gzip > pg-image.tar.gz

# 目标机器：load 两个镜像，把 compose 里的 build: 改成 image: dszoon:0.1.0
docker compose --env-file .env up -d
```



---

## 6. 形态 D：Windows Server

依赖全是纯 Python 或有官方 wheel（asyncssh / paramiko / cryptography），Windows 直接可用。

```powershell
python -m venv venv
.\venv\Scripts\pip install ".[web]"
cd web; npm install; npm run build; cd ..

# 注册服务（二选一）
nssm install dszoon "C:\dszoon\venv\Scripts\uvicorn.exe" `
  "app.main:create_app --factory --host 0.0.0.0 --port 8000"
# 或 WinSW
```

两个 Windows 特有的注意点：

- `DSZOON_DATA_DIR` 用**绝对路径**（如 `D:\dszoon\var`），别用相对的 `var`——  
  服务方式启动时工作目录未必是你以为的那个；
- `.env` 在 Windows 上不受 `chmod 0600` 保护，**两个密钥改用系统环境变量设置**  
  （`setx /M` 或服务配置里的环境变量）。

---

## 7. 形态 E：本地试用（开发机）

```bash
cd backend
python -m venv .venv && .\.venv\Scripts\Activate.ps1   # Linux: source .venv/bin/activate
pip install -e ".[dev,web]"
python scripts/preflight.py     # ★ 先跑这个，10 秒体检，不要先跑 pytest
python -m app.cli init-db
uvicorn app.main:create_app --factory --reload --port 8000
```

前端开发模式另行 `cd web && npm install && npm run dev`（默认 5173，已在 CORS 白名单里）。

> 沿用旧版本的开发库会踩坑：`create_all` **不给已有表加列**，症状是"启动正常，  
> 一打开设备页整页 500（`no such column: devices.group_id`）"。  
> 开发库直接删掉重建最简单：删 `var/dszoon.db`，**保留 `var/.secrets.json`**  
> （删了它设备密码就解不开了）。`preflight.py` 会提前把这件事报出来。

---

## 8. 环境变量总表

全部以 `DSZOON_` 为前缀，可用环境变量或 `.env` 文件设置。

### 存储与密钥

| 变量                                | 默认                                    | 说明                              |
| --------------------------------- | ------------------------------------- | ------------------------------- |
| `DSZOON_DATABASE_URL`             | `sqlite+aiosqlite:///./var/dszoon.db` | 生产用 `postgresql+asyncpg://...`  |
| `DSZOON_DATA_DIR`                 | `var`                                 | 配置正文与会话日志根目录，**必须备份**           |
| `DSZOON_AUTO_CREATE_SCHEMA`       | `true`                                | 见 §2.1，正式环境设 `false`            |
| `DSZOON_SECRET_KEY`               | 空（自动生成）                               | 设备凭据加密密钥，**改了要重录密码**            |
| `DSZOON_JWT_SECRET`               | 空（自动生成）                               | JWT 签名密钥                        |
| `DSZOON_ACCESS_TOKEN_TTL_MINUTES` | `720`                                 | 登录有效期                           |
| `DSZOON_ADMIN_USERNAME`           | `admin`                               | 初始管理员                           |
| `DSZOON_ADMIN_PASSWORD`           | 空                                     | 留空则生成随机口令，**仅在首次建库时生效**并在日志打印一次 |


### 采集

| 变量                          | 默认      | 说明                  |
| --------------------------- | ------- | ------------------- |
| `DSZOON_BACKUP_CONCURRENCY` | `20`    | 全局并发采集数             |
| `DSZOON_BACKUP_RETRIES`     | `2`     | 单设备重试次数             |
| `DSZOON_BACKUP_PRECHECK`    | `true`  | 采集前连通性预检            |
| `DSZOON_CONNECT_TIMEOUT`    | `15.0`  | 连接超时（秒）             |
| `DSZOON_COMMAND_TIMEOUT`    | `180.0` | 单命令超时（秒）——大配置慢设备要调大 |
| `DSZOON_IDLE_TIMEOUT`       | `25.0`  | 空闲超时（秒）             |
| `DSZOON_KEEP_SESSION_LOGS`  | `true`  | 保存原始会话日志（排障用，占磁盘）   |

### 调度与保留（P2）

| 变量                                | 默认     | 说明                            |
| --------------------------------- | ------ | ----------------------------- |
| `DSZOON_SCHEDULER_ENABLED`        | `true` | **多实例部署时只让一个实例开**，否则同一策略被重复执行 |
| `DSZOON_SCHEDULER_POLL_SECONDS`   | `30`   | 轮询间隔；精度受它限制，对"每天凌晨备份"足够       |
| `DSZOON_RETENTION_INTERVAL_HOURS` | `24`   | 保留策略自动清理间隔；`0` = 只能手工点"立即清理"  |

> 别轻易把 `retention_interval_hours` 设成 0：只靠人点的清理在真实环境里等于**永远不跑**，  
> 版本会一直堆到磁盘满。自动清理挂在调度器轮询里，所以 `scheduler_enabled=false` 时  
> **不会**自动清理，界面上仍可手工触发。

### 合规（P3）

| 变量                          | 默认     | 说明                                      |
| --------------------------- | ------ | --------------------------------------- |
| `DSZOON_COMPLIANCE_ENABLED` | `true` | 采集到新版本后自动跑基线；关掉只能手工"立即检查"               |
| `DSZOON_COMPLIANCE_RULES`   | `[]`   | 自定义规则文件路径（JSON 数组）。给了就**整体替换**内置规则，不是叠加 |

内置基线在 `app/compliance/baselines/baseline.yaml`，共 16 条规则  
（8 条 high 违规即 fail 并发通知、7 条 medium、1 条 low）。  
它是**起点不是标准答案**：等保 2.0 / 行业规范 / 客户内部基线各不相同，按现场要求增删后  
用 `DSZOON_COMPLIANCE_RULES` 指向自己的 YAML 整体替换。  
规则文件是打进镜像/离线包的，自定义规则请挂载卷或用绝对路径。

### 配置下发与正文存储（P4）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DSZOON_DEPLOY_ENABLED` | `false` | 配置下发总开关。**默认关闭是刻意的**：P0~P3 只读设备，最坏是"没采到"；下发往生产设备写配置，最坏是**把网络改坏**。开启是一次要有记录的人为决定，不是升级就自动生效 |
| `DSZOON_DEPLOY_ALLOW_SELF_APPROVAL` | `false` | 是否允许申请人审批自己的变更单。默认必须**第二人审批**；打开等于"谁申请谁批准"，事后追责链断掉一半 |
| `DSZOON_BLOB_BACKEND` | `filesystem` | `filesystem` 或 `git`。`git` = 在内容寻址目录之外，把每个**新对象**提交进一个仓库，让"这份配置哪来的、什么时候进来的"有一份独立于数据库的证据（`git log`）。写错的值会**直接报错**，不会静默退回文件系统 |
| `DSZOON_GIT_REPO` | 空 | git 后端的仓库路径。**对象目录必须位于该仓库内**（对象在 `<数据目录>/objects/`，所以仓库根设成数据目录即可）。不满足时启动直接报错 |

> 开启下发后，本接口层**仍只能预演**：P4-A 的驱动层（进卷积、逐行回显确认、`commit`）
> 尚未接入，`POST /changes/{id}/preview` 会完整跑一遍闸门但一行都不发。
> 这是有意的——留一条"看起来能发、实际发得不对"的路由，比没有更危险。

> `git` 后端不改变对象的存放位置与文件名，只**附加**提交历史，所以随时可开可关，
> 不存在读不出来的迁移问题。同一份内容（内容寻址天然去重）只会产生一个提交，
> 不会因为每天几百次轮询把 `git log` 刷成噪声。

### 其它

| 变量                    | 默认                          | 说明                     |
| --------------------- | --------------------------- | ---------------------- |
| `DSZOON_DEBUG`        | `false`                     |                        |
| `DSZOON_CORS_ORIGINS` | `["http://localhost:5173"]` | 同源部署（前后端同一端口）设 `[]` 即可 |
| `DSZOON_APP_NAME`     | `dszoon`                    |                        |

---

## 9. 网络与端口

| 方向         | 端口                      | 说明                            |
| ---------- | ----------------------- | ----------------------------- |
| 应用 → 设备管理网 | **22/tcp (SSH)**        | 采集方向，**必须放通**；不能用跳板机替代        |
| 应用 → 设备管理网 | 23/tcp (Telnet)         | 未实现，预留                        |
| 用户 → 应用    | 8000（直连）或 80/443（nginx） | 建议只开 443                      |
| 应用 → 数据库   | 5432/tcp                | 同机或内网                         |
| 应用 → 通知渠道  | 443/tcp                 | 钉钉/企微/飞书/Webhook/SMTP；不通只影响通知 |
| 监控 → 应用    | 8000 `/api/v1/metrics`  | **不要求登录**，见 §15               |

设备侧只需要一个**具备只读权限的账号**（能执行 `display current-configuration` 之类的查看命令）。  
不要给它配置权限。

---

## 10. 首次登录与初始化

1. 浏览器打开 `http://<IP>:8000`，用 `admin` + `DSZOON_ADMIN_PASSWORD` 登录。  
   没设 `DSZOON_ADMIN_PASSWORD` 时，口令在**首次建库时的启动日志**里打印一次  
   （`docker compose logs app` / `journalctl -u dszoon`），只打印这一次。
   > 忘了口令：`python -m app.cli reset-password admin`。
2. **立刻改口令**（界面右上角）。初始口令是明文出现在日志里的。
3. 加设备：填 IP、厂商（华为 VRP / 华三 Comware / 锐捷 RGOS）、SSH 账号口令。  
   先用界面的"连通性预检"确认可达，再跑采集。
4. 跑一次手工备份，确认拿到版本、能看到内容与差异。
5. 建定时策略（如每天 02:00）与保留策略，并确认 `TZ` 正确（§8）。
6. 配通知渠道（可选），点"测试发送"确认能通。
7. 到"合规"页跑一次基线，确认能出结论。

命令行等价的几个常用操作：

```bash
python -m app.cli init-db          # 开发建表（生产用 alembic upgrade head）
python -m app.cli gen-key          # 生成设备凭据加密密钥
python -m app.cli create-user ops --role operator
python -m app.cli reset-password admin
python -m app.cli list-devices
python -m app.cli backup --all     # 不开界面跑一次备份，适合挂 cron
```

---

## 11. 备份与恢复

### 11.1 要备份的三样（缺一不可）

| 对象   | 命令                                                             | 丢了会怎样                |
| ---- | -------------------------------------------------------------- | -------------------- |
| 数据库  | `pg_dump -U dszoon dszoon > dszoon-$(date +%F).sql`            | 设备台账、任务历史、版本索引全丢     |
| 数据目录 | `tar -czf data-$(date +%F).tgz -C <data_dir> objects sessions` | **历史配置内容**全丢（库里只剩指针） |
| 两个密钥 | 抄走 `.env` 或 `$DSZOON_DATA_DIR/.secrets.json`                   | 设备密码解不开，要逐台重录        |

SQLite 部署：**停服后**直接复制 `var/dszoon.db` 最稳妥（运行中复制可能拿到不一致的快照）。

三样必须是**同一时间点**的。恢复一个 8-01 的库配一份 8-15 的数据目录，会出现  
"列表里有这个版本、点开内容不存在"。

### 11.2 恢复步骤

```bash
systemctl stop dszoon                       # 或 docker compose down
# 1) 恢复数据库
psql -U dszoon -d dszoon < dszoon-2026-08-15.sql
# 2) 恢复数据目录（路径要与原部署一致）
tar -xzf data-2026-08-15.tgz -C /opt/dszoon/backend/var
# 3) 恢复密钥（与备份里的一致，别重新生成）
install -m 600 /备份/dszoon.env /etc/dszoon.env
# 4) 起服并验证
systemctl start dszoon && curl -s localhost:8000/readyz
python scripts/selfcheck.py
```

恢复后必做的**一致性抽查**：随机点开两三台设备的历史版本，确认内容能正常显示；  
再确认最新版本的时间与备份时间吻合。文件缺失会在版本详情里直接报错，不会静默。

---

## 12. 升级

### 12.1 标准流程

```bash
# 1) 先备份（§11.1 三样）
# 2) 停服
systemctl stop dszoon
# 3) 换代码 / 前端产物
cd /opt/dszoon/backend && git pull        # 或解压新版本
cd web && npm ci && npm run build && cd ..
# 4) 装新依赖
/opt/dszoon/venv/bin/pip install ".[web]"
# 5) 应用迁移（AUTO_CREATE_SCHEMA=false 时必做）
/opt/dszoon/venv/bin/alembic upgrade head
# 6) 起服并验收
systemctl start dszoon && python scripts/selfcheck.py
```

Docker 部署把第 2~6 步换成：

```bash
docker compose down
docker compose build --pull && docker compose up -d
docker compose exec app alembic upgrade head
```

> ★ 只换前端页面也要 `npm run build`。忘了构建的症状是"界面上还是老样子"，  
> 启动日志里会有一条"前端产物比源码旧"的提醒。

### 12.2 从 `create_all` 切到 Alembic（老库）

已经用 `auto_create_schema=true` 跑了一段时间、现在想交给 Alembic 管：

```bash
# 1) 先确认 schema 与当前代码一致：启动一次，若报"schema 比代码旧：缺少 xxx"就
#    说明真的有列没加，先按 §12.1 的方式补齐（或重建库），不要往下走
# 2) 记录当前版本（不执行任何 DDL，只写 alembic_version）
alembic stamp head
# 3) 把 DSZOON_AUTO_CREATE_SCHEMA 改成 false 并重启
```

`stamp` 是"声明这个库已经是 head 的样子"。**如果 schema 其实落后，stamp 会把问题藏起来**  
——所以第 1 步的漂移检查必须先过。

---

## 13. 日常运维

### 日志

| 部署形态    | 命令                           |
| ------- | ---------------------------- |
| systemd | `journalctl -u dszoon -f`    |
| Docker  | `docker compose logs -f app` |

日志走 stdout，格式统一为 `时间 级别 logger: 消息`。启动时留意三类日志：  
挂载的前端目录、自动生成密钥的告警、管理员口令（仅首次）。

### 健康检查与指标

| 端点                    | 用途                         | 鉴权    |
| --------------------- | -------------------------- | ----- |
| `GET /healthz`        | 存活（进程在不在）                  | 无     |
| `GET /readyz`         | 就绪（含数据库 ping），DB 不可达返回 503 | 无     |
| `GET /api/v1/metrics` | Prometheus 文本格式            | **无** |

Prometheus 抓取配置：

```yaml
scrape_configs:
  - job_name: dszoon
    metrics_path: /api/v1/metrics
    static_configs:
      - targets: ["10.0.0.10:8000"]
```

主要指标（标签基数都是有界的，可以直接进时序库）：

| 指标                                                   | 类型            | 看什么                          |
| ---------------------------------------------------- | ------------- | ---------------------------- |
| `dszoon_up`                                          | gauge         | 1 = 数据库可读                    |
| `dszoon_devices_total{enabled}`                      | gauge         | 台账数量                         |
| `dszoon_snapshots_total`                             | gauge         | 配置版本总数                       |
| `dszoon_backup_runs_total{status}`                   | counter       | 采集结果分布，`unreachable` = 设备不可达 |
| `dszoon_backup_run_duration_seconds_sum/_count/_max` | counter/gauge | 采集耗时                         |
| `dszoon_backup_tasks_total{status}`                  | gauge         | 任务状态分布                       |
| `dszoon_schedules_enabled_total`                     | gauge         | 启用中的定时策略数                    |
| `dszoon_schedules_due_total`                         | gauge         | **持续 >0 = 调度器没在跑**，最值得告警的一条  |
| `dszoon_compliance_results_total{verdict}`           | gauge         | 合规判定分布                       |
| `dszoon_compliance_high_findings_total`              | gauge         | 最新一轮 high 级违规总数              |
| `dszoon_notifications_total{status}`                 | counter       | 通知发送结果                       |

建议的两条告警：`dszoon_up == 0`、`dszoon_schedules_due_total > 0` 持续 10 分钟。

### 日常巡检清单

| 频率  | 动作                                           |
| --- | -------------------------------------------- |
| 每天  | 看备份任务是否全绿；`dszoon_schedules_due_total` 是否为 0 |
| 每周  | 抽查 2~3 台设备的版本是否有新内容；看磁盘占用                    |
| 每月  | 抽查一次恢复演练（至少恢复数据库 + 一个版本的内容）                  |
| 每季度 | 复核设备台账（下线设备停用）、复核保留策略是否与制度一致                 |

---

## 14. 多实例与高可用

**当前版本不支持多实例同时跑调度。**

调度器是进程内的轮询循环，数据库是唯一事实来源（没有内存 job 同步问题，这是刻意的设计），  
但它**没有分布式锁**。两个实例同时开调度器 = 同一策略被执行两次 = 重复采集与重复通知。

现在的过渡做法：

- 多实例只用于"多个只读 Web 前端"，其中**只有一个实例设 `DSZOON_SCHEDULER_ENABLED=true`**；
- 或用 nginx 只把写操作路由到一个实例（配置复杂度高，不推荐）；
- 用 `dszoon_backup_runs_total` 的增速判断有没有真的重复执行。

横向扩展与分布式执行计划在 P4（Celery），落地后本节会改写。

---

## 15. 安全清单

部署完成前逐项确认：

- [ ] `DSZOON_SECRET_KEY` / `DSZOON_JWT_SECRET` 是**显式设置**的，不是自动生成的
- [ ] 密钥文件权限 0600、属主是运行用户；两个密钥已进入备份
- [ ] 初始管理员口令已修改；日志里的那一次打印已经过去
- [ ] 设备凭据用的是**只读账号**（设备侧不授予配置权限）
- [ ] 数据库口令不是默认值，且数据库只监听内网
- [ ] `/api/v1/metrics`、`/healthz`、`/readyz`、`/docs` **都不要求登录** →  
  用 nginx 限源网段，或至少不要暴露到公网
- [ ] 对外走 HTTPS（证书放 `deploy/certs/`），8000 端口不对外
- [ ] 容器以非 root（uid 10001）运行；systemd 加固项未被削弱
- [ ] 审计日志已确认在记录（敏感读取、导出都会被审计，导出需 auditor 及以上角色）
- [ ] `GET /api/v1/search/config`（跨设备明文检索）只给了该给的人：它需要 **auditor 及以上**，
      且**每次搜索都写审计**（查询词进 `detail`）。这是全库明文检索，权限最高的一次读
- [ ] `DSZOON_DEPLOY_ENABLED` 保持 `false`，除非已读完 `DEVELOPMENT_PLAN.md` §8.5.1 并落实了审批人；
      开启后本版本**仍只能预演**（P4-A 驱动层未接入），界面上不该出现"已经能下发"的暗示
- [ ] 角色分层按最小权限分配：viewer < auditor < operator < admin（权限是**包含**关系，  
  operator 可读审计日志）

---

## 16. 故障处置表

按报错原文查。

| 现象 / 报错                                            | 原因                                | 处置                                                                                       |
| -------------------------------------------------- | --------------------------------- | ---------------------------------------------------------------------------------------- |
| `数据库里没有 users 表，但 DSZOON_AUTO_CREATE_SCHEMA=false` | 库里还没建表                            | `alembic upgrade head`；开发环境可把该开关设回 `true`                                                |
| `数据库 schema 比代码旧：缺少 devices.group_id`              | 沿用旧库，`create_all` 只补表不加列          | PostgreSQL：`alembic upgrade head`；开发 SQLite：删 `var/dszoon.db` 重建（保留 `var/.secrets.json`） |
| 启动日志 `已自动生成密钥并保存到 …`                               | 没设密钥                              | 正式环境必须显式设置并备份；换 `secret_key` 会导致设备密码全部失效                                                 |
| 设备页整页 500                                          | 同"schema 比代码旧"                    | 同上                                                                                       |
| 界面 404 / 只有 API、`/docs` 能开                         | 前端产物没构建，或工作目录不对                   | `cd web && npm run build`；确认服务从 `backend/` 启动；看启动日志里挂载的目录                                |
| 界面还是老样子                                            | `web/dist` 比源码旧                   | 重新 `npm run build`                                                                       |
| `readyz` 返回 503                                    | 数据库连不上                            | 查 `DSZOON_DATABASE_URL`、库是否起来、网络与口令；compose 下等 db 健康检查通过                                 |
| 定时任务到点不跑                                           | 调度器没开 / 宕了                        | 查 `dszoon_schedules_due_total`；确认该实例 `DSZOON_SCHEDULER_ENABLED=true`；看日志                 |
| 同一策略被执行多次                                          | 多实例都开了调度器                         | 只保留一个实例开启（§14）                                                                           |
| cron 时间偏了 8 小时                                     | `TZ` 未设置                          | 设 `TZ=Asia/Shanghai` 并重启（compose 里要透传）                                                   |
| 保留策略不自动清理                                          | 调度器关闭，或间隔为 0                      | 开调度器；`DSZOON_RETENTION_INTERVAL_HOURS>0`；也可在界面手工"立即清理"                                   |
| 合规不自动跑                                             | `DSZOON_COMPLIANCE_ENABLED=false` | 改回 `true`，或用界面"立即检查"                                                                     |
| 改了 `.env` 但容器里不生效                                  | compose 是显式列环境变量                  | 在 `docker-compose.yml` 的 `app.environment` 里补那一行                                         |
| 任务进度条不动 / 事件最后一次性刷出                                | 反向代理缓冲了 SSE                       | 确认 nginx 用了仓库里的 `nginx.conf`（`proxy_buffering off`）                                      |
| 大配置采集超时/内容不全                                       | 命令或空闲超时太短                         | 调大 `DSZOON_COMMAND_TIMEOUT` / `DSZOON_IDLE_TIMEOUT`                                      |
| 设备连不上（预检失败）                                        | 22 端口未放通，或设备算法老                   | 放通 22；老设备走 paramiko 兜底路径；确认账号权限                                                          |
| systemd 启动正常但备份写文件失败                               | `ReadWritePaths` 未含数据目录           | 同步 unit 里的 `ReadWritePaths`（§4）                                                          |
| 页面提示"版本内容不存在"                                      | 数据目录与数据库不是同一时间点                   | 从同一份备份成对恢复（§11）                                                                          |

排障时优先跑 `python scripts/preflight.py`（10 秒只读体检）和 `python scripts/selfcheck.py`  
（分层自检，能定位到具体某一层），细节见 `docs/P1_RUNBOOK.md`。

---

## 17. 容量估算

配置正文是**内容寻址**的：内容没变就不产生新对象，只多一条指向旧内容的记录。  
所以空间增长主要取决于"配置真的变了多少次"，而不是"采了多少次"。

粗算：

```
占用 ≈ 设备数 × 单台配置大小 × 保留期内实际变更次数 × 1.3（元数据与碎片）
```

参考量级：单台交换机/路由器配置通常几十 KB ~ 几百 KB；100 台设备、  
保留 180 天、平均每周变更 1 次 → 约 100 × 100KB × 26 × 1.3 ≈ **340 MB**。

需要额外留出的：

- 会话日志（`DSZOON_KEEP_SESSION_LOGS=true` 时每台每次一份原始回显，通常比配置大几倍；  
  磁盘紧张就关掉）；
- 数据库本身（任务历史与版本索引，量级远小于配置正文）。

建议：给数据目录留出估算值的 **3 倍**余量，并把 `dszoon_snapshots_total` 与磁盘占用  
一起纳入监控；磁盘到 80% 前先调小保留期，而不是删库。

---

## 18. 部署验收清单

- [ ] 服务能被 systemd/Docker 托管，重启后自动恢复
- [ ] `/healthz` 返回 200，`/readyz` 的 `database=true`
- [ ] `python scripts/selfcheck.py` 全绿
- [ ] 浏览器能打开界面并登录，改掉初始口令
- [ ] 至少一台真机完成一次采集，版本内容能在界面查看与比对
- [ ] 定时策略能按预期时间触发（改一次测试时间验证过）
- [ ] 保留策略按预期生效（`retention_interval_hours` 生效或手工清理成功）
- [ ] 合规基线能出结论（至少有一台设备有判定结果）
- [ ] 通知渠道"测试发送"成功（若启用）
- [ ] `/api/v1/metrics` 能被 Prometheus 抓到，两条建议告警已配置
- [ ] 完成一次备份，并**实际演练过一次恢复**（含数据目录与密钥）
- [ ] 确认当前版本对设备只读（§0），运维侧不按"能下发配置"使用
