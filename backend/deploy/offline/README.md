# 离线（内网隔离）部署

政企内网通常没有外网，`pip install` / `npm install` 都跑不通。做法是**在能上网的机器上  
打包，再到目标机器离线安装**。

## 1. 在联网机器上打包

### 1.1 先确认平台对得上（**最容易翻车的一步**）

`wheels/` 里的轮子与**操作系统 + 架构 + Python 小版本**三者绑定，  
三个都对上才能装。`cryptography` / `asyncpg` / `pydantic-core` 都是编译扩展，  
平台不对时目标机的 `pip install` 会报"找不到匹配的版本"——那句话不会告诉你  
"这个包是为 Windows 打的"。

| 打包机          | 目标机          | 怎么做                                                                             |
| ------------ | ------------ | ------------------------------------------------------------------------------- |
| Linux x86_64 | Linux x86_64 | ✅ `make_bundle.sh`，最顺                                                           |
| Windows x64  | Windows x64  | ✅ `make_bundle.ps1 -TargetOs native`                                            |
| 任意           | 任意（**推荐**）   | ✅ Docker 镜像包（见 §4），彻底绕开 wheel 平台问题                                              |
| Windows x64  | Linux x86_64 | ⚠ `make_bundle.ps1 -TargetOs linux`（跨平台下载，未真机验证）；或在 Linux 容器里跑 `make_bundle.sh` |
| Linux x86_64 | Windows x64  | ❌ 不要在 Linux 上打 Windows 包，改用目标机自己联网装                                             |

Python **小版本**也要一致（3.12 ≠ 3.13）。打包器会把环境写进包里的  
`PACKAGING.txt`，目标机的 `install.sh` 会拿它跟本机对比并给出提示。

### 1.2 打包

机器要求：有 Python（建议直接用仓库里的 `.venv`）、Node.js 20+、npm。

```bash
# Linux / macOS / WSL / Git Bash
cd backend
bash deploy/offline/make_bundle.sh
# 产物：deploy/offline/dist/dszoon-offline-<日期>.tar.gz
```

```powershell
# Windows（PowerShell 7 或 5.1 都行）
cd backend
pwsh -File deploy\offline\make_bundle.ps1
# 为 Linux 目标机打包：
pwsh -File deploy\offline\make_bundle.ps1 -TargetOs linux -TargetPython 3.12
```

### 1.3 包里装了什么

```
dszoon-offline-<日期>/
  wheels/         所有 Python 依赖的 wheel（离线 pip 用）
  web-dist/       前端构建产物（后端自动挂载到 /）
  app/            后端源码
  scripts/        自检与真机探针（selfcheck.py / preflight.py / smoke.py）
  tests/          自检与冒烟用的夹具与假设备（★ 不能少，见下）
  migrations/     Alembic 迁移 + alembic.ini
  deploy/         Dockerfile / docker-compose.yml / nginx.conf / systemd / 本文件
  pyproject.toml
  PACKAGING.txt   打包机的 OS / 架构 / Python 版本（装前核对用）
  install.sh      目标机上一条命令装完
```

`tests/` 必须带着：`scripts/selfcheck.py` 会 `import tests.support.fake_device`  
并读 `tests/fixtures` 下的样本回显。少了它，"装完先自检"这一步会直接崩，  
而那时你已经在没有外网的现场了。

## 2. 拷到目标机器并安装

```bash
tar -xzf dszoon-offline-<日期>.tar.gz
cd dszoon-offline-<日期>
bash install.sh          # 核对打包环境 → 建 venv → 离线装轮子 → 放前端产物 → 生成 .env
./venv/bin/python scripts/selfcheck.py    # ★ 装完先自检：用假设备端到端跑一遍
./venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

`install.sh` 第一件事是拿 `PACKAGING.txt` 与本机 Python 版本对比，不一致就直接提示  
——因为接下来 pip 的报错信息里看不出"这个包是为别的平台打的"。

`install.sh` 生成的 `.env` 里有自动生成的 `DSZOON_SECRET_KEY` 与 `DSZOON_JWT_SECRET`：

> **立刻备份这两个密钥**。`secret_key` 是设备凭据的加密密钥，  
> 丢了/换了 → 所有设备密码都要重新录入（数据库里的密文解不开了）。

## 3. 用 PostgreSQL 时

离线机器上装 PostgreSQL 16，然后：

```bash
DSZOON_DATABASE_URL='postgresql+asyncpg://dszoon:口令@127.0.0.1:5432/dszoon' \
  ./venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

`asyncpg` 已包含在 `[web]` extras 里，无需额外安装。

## 4. 用 Docker 时

联网机器上：

```bash
cd backend
docker build -f deploy/Dockerfile -t dszoon:0.1.0 .
docker save dszoon:0.1.0 | gzip > dszoon-image.tar.gz
# 数据库镜像也一起带上
docker pull postgres:16-alpine && docker save postgres:16-alpine | gzip > pg-image.tar.gz
```

目标机器上：

```bash
gunzip -c dszoon-image.tar.gz | docker load
gunzip -c pg-image.tar.gz | docker load
# 用 deploy/docker-compose.yml（把 build: 改成 image: dszoon:0.1.0）
docker compose --env-file .env up -d
```

## 5. systemd 常驻

见 `deploy/systemd/dszoon.service`，文件末尾有完整安装步骤。  
要点：密钥放 `/etc/dszoon.env`（`chmod 600`），**不要写在命令行里**（`ps` 能看到）。

## 6. 升级与备份

升级：

```bash
# 停服 → 换代码/前端产物 → 装新依赖 → 起服
./venv/bin/pip install --no-index --find-links wheels -e .
```

备份（两样都不能少）：

| 备份对象              | 内容                            | 丢了会怎样                  |
| ----------------- | ----------------------------- | ---------------------- |
| 数据库               | `pg_dump dszoon > dszoon.sql` | 设备台账、任务历史、版本索引全丢       |
| `DSZOON_DATA_DIR` | 配置正文与会话日志                     | **历史配置内容**全丢（数据库里只有指针） |
| `secret_key`      | `.env` / `.secrets.json`      | 设备密码解不开，要重录            |

## 7. 网络要求

| 方向         | 端口                        | 说明        |
| ---------- | ------------------------- | --------- |
| 应用 → 设备管理网 | 22（SSH）/ 23（Telnet，暂未实现）  | 采集方向，必须放通 |
| 用户 → 应用    | 80 / 443（nginx）或 8000（直连） | 建议走 HTTPS |

## 8. Windows Server 部署

Python 依赖都是纯 Python 或有官方 wheel（asyncssh / paramiko / cryptography），  
在 Windows 上直接可用：

```powershell
python -m venv venv
.\venv\Scripts\pip install --no-index --find-links wheels -e .
# 用 WinSW / NSSM 注册成服务：
#   winsw install 或 nssm install dszoon "C:\dszoon\venv\Scripts\uvicorn.exe" `
#        "app.main:create_app --factory --host 0.0.0.0 --port 8000"
```

注意 Windows 上的 `DSZOON_DATA_DIR` 用绝对路径，且 `.env` 文件权限不受 chmod 保护，  
建议改用系统环境变量设置两个密钥。
