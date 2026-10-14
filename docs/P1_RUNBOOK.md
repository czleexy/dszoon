# P1 验证 Runbook

> **为什么有这份文档**：P1 的代码、前端、部署文件都已写完，但**开发环境里无法执行任何命令**
> （shell 进程启动即失败，退出码 `0xC0000142`），所以全部代码从未在真实解释器里跑过。
> 验证只能由人在一台能跑的机器上完成。这份文档把"跑什么、每一步证明什么、失败了怎么分类"
> 一次写清楚，不依赖任何上下文。
>
> 按顺序执行即可。**每一步失败都先看第 7 节（失败特征对照表）**，多数情况下能直接定位。

---

## 当前状态（从 `.venv`、`.pytest_cache` 与实际运行结果读出来的证据）

### 已验证 ✅

| 项 | 证据 |
|---|---|
| **P0 采集层测试通过**（约 111 个用例） | `.pytest_cache/v/cache/lastfailed` 里只剩指向已重命名/删除测试的陈旧记录，真实失败已被后续运行清除 |
| **P1 API 层已跑通** | 全量运行 `153 passed, 3 failed`——接口层、数据库、后台任务、SSE、任务取消、版本对比、脱敏、审计全部跑起来了。3 个失败已定位并修复（见下） |
| 依赖安装成功 | `site-packages/dszoon_backup-0.1.0.dist-info` 存在（editable 安装） |
| 版本兼容性无问题 | 之前担心的 pytest 9 / pytest-asyncio 1.4 / SQLAlchemy 2.1 / starlette 1.7 **均未造成问题** |
| 华为 VRP5 真机探针跑过 | `.spike/HW-CORE-01/report.md` |

### 首次全量运行暴露出的 3 个问题（已修，待重跑确认）

| # | 现象 | 根因 | 修法 |
|---|---|---|---|
| 1 | 连接测试返回 `unreachable`，且耗时 15 秒 | `/devices/{id}/test` **没把注入的会话工厂传给 `probe_device`**，于是绕过测试注入去发真实 SSH 连接 | `app.state.session_factory` + `SessionFactoryDep` 依赖 + 端点传入；selfcheck 第 7 步增加连接测试检查 |
| 2 | 批量备份里"某台没绑凭据"导致整批失败 | `build_targets()` 对整批抛异常 → 50 台里 1 台没配好，另外 49 台也一台不采 | 改为返回 `(可采集目标, 不可采集清单)`；缺凭据的设备各记一条失败记录，其余照常采集；单设备 `/test` 仍返回 422 |
| 3 | teardown 出现 `database is locked` | SQLite 默认 rollback journal 下，请求会话与后台任务会话互相阻塞；取消任务后收尾写状态会失败（任务永远停在 running） | 打开 `PRAGMA journal_mode=WAL` + `busy_timeout=5000`；并让 `cancel()` 等到终态写完再返回 |

### 仍未执行 ❌

| 项 | 证据 |
|---|---|
| **应用启动**（`create_app` / 建表 / 挂载前端 / 生命周期） | 没有 `backend/var/`、没有 `.secrets.json` → 从未运行过 `uvicorn` 或 `init-db` |
| **前端构建** | 已构建过（`web/node_modules` 与 `web/dist` 都存在）；但 P2 新增了 4 个页面与路由，**需要重新 `npm run build`** 才会出现在 `dist` 里 |
| 华三 / 锐捷真机实测 | `docs/SPIKE_REPORT.md` 里这两栏仍是空的 |

> P2（定时调度/保留策略/通知渠道/设备分组/1000 台压测）的验证步骤另见
> **`docs/P2_ACCEPTANCE.md`**。

### 环境基线（实际安装版本 vs 代码写作时的假设）

| 包 | 实际版本 | 需要注意 |
|---|---|---|
| pytest | **9.1.1** | 高于 `pyproject.toml` 里的 `>=8.2`，是大版本 |
| pytest-asyncio | **1.4.0** | **1.x**：`asyncio_mode="auto"` 仍支持；`event_loop` fixture 已移除（本项目未用） |
| SQLAlchemy | **2.1.4** | **2.1**（代码按 2.0 写的，`Mapped[]`/`mapped_column` 用法未变） |
| FastAPI / starlette | 0.143.0 / **1.7.0** | starlette 也进了 **1.x** |
| pydantic / pydantic-settings | 2.14.0 / 2.15.0 | v2 ✓ |
| httpx | 0.28.1 | 用 `ASGITransport(app=...)`（0.28 移除了 AsyncClient 的 `app=` 快捷方式，本项目未用） |
| paramiko | **5.0.0** | 只用 `SSHClient`/`AutoAddPolicy`/`invoke_shell` 等基础 API |
| asyncssh | 2.24.1 | 主会话实现，只用 `connect`/`create_process` |
| alembic / aiosqlite / asyncpg | 1.20.0 / 0.22.1 / 0.32.0 | |

> 若 P1 层出现读起来莫名其妙的报错（尤其依赖注入、mapper 配置、async 夹具），
> **优先怀疑上面这几个大版本**：pytest 9、pytest-asyncio 1.x、SQLAlchemy 2.1、starlette 1.x。
> 它们的版本都高于代码写作时的假设。

---

## 0. 一次性准备

```powershell
cd F:\dszoon\backend
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,web]"
```

> ⚠ **用激活后的 `python`，不要用裸 `pytest`**。
> 裸 `pytest` 走环境里的 `pytest.exe`，未必和 `python` 是同一个解释器——这会表现为
> "测试能跑但脚本报 `No module named 'yaml'`"。用 `python -m pytest` 可从根本上避免。
> 自查：`python -c "import sys, yaml, asyncssh, fastapi; print(sys.executable)"`

---

## 1. 后端自检（**先跑这个，不要先跑 pytest**）

```powershell
python scripts/selfcheck.py
```

8 步逐级验证，失败在哪一层立刻可见：

| 步 | 验证内容 | 失败意味着 |
|---|---|---|
| 1 | 运行环境 | 依赖没装齐（含 P2 新增的 APScheduler），或装到了另一个解释器 |
| 2 | 语法编译（compileall） | 有语法错误（会指出具体文件） |
| 3 | 模块导入（遍历 `app.*`） | 某个模块导入失败——**一次列出全部**，这正是 pytest 做不到的 |
| 4 | 驱动与清洗层 | 提示符正则编译失败 / 分页、ANSI、回显剥离行为异常 |
| 5 | CLI 与迁移配置 | `alembic.ini` 转义错误、CLI 参数定义问题 |
| 6 | 建库 + 初始管理员 | SQLAlchemy 注解解析 / mapper 配置 / 建表失败 |
| 7 | 端到端（登录→建设备→备份→落库→脱敏→diff） | 接口 → 服务 → 后台任务 → 数据库这条链有问题 |
| 8 | **P2 运营链路**（分组 / 定时策略 / 预览 / 手工执行 / 渠道脱敏与测试 / 保留策略与清理 / 审计动作） | P2 的接口与服务层有问题；**发送环节是打桩的，不会真往群里发消息** |

**第 3 步是关键**：pytest 遇到 conftest 导入错误就停，只暴露第一个问题；selfcheck 会把当前
所有导入错误一次性列出来，省掉来回几轮。

---

## 2. 完整回归

```powershell
python -m pytest -q
```

用例分布（按目录）：

| 目录 | 覆盖 |
|---|---|
| `tests/unit/` | 文本清洗、完整性校验、驱动（提示符/命令表/脱敏/facts）、包级导出 |
| `tests/integration/` | 引擎 × 假设备 SSH 服务器（分页/ANSI/GBK/断连/卡死/内容截断/[Y/N] 确认） |
| `tests/api/` | 鉴权与 RBAC、设备与凭据 CRUD、CSV 导入、备份任务与落库、版本 diff、SSE 流、任务取消、审计 |

期望：全部通过。若有失败，记下**测试名 + 断言消息**即可（不需要贴全部输出）。

---

## 3. 前端构建

```powershell
cd web
npm install
npm run build
cd ..
```

期望产物：`web/dist/index.html` + `web/dist/assets/*`。
构建成功后，后端会自动挂载这个目录（`app/main.py` 末尾），**同一个端口同时提供前后端**。

---

## 4. 起服务并冒烟（不需要真机）

```powershell
uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

- 注意 `--factory`：`create_app` 是工厂函数，不是模块级 `app` 实例
- **看启动日志**：首次运行会打印一次 `admin` 的随机口令（或用 `DSZOON_ADMIN_PASSWORD` 指定）
- 浏览器打开 http://127.0.0.1:8000 → 登录 → 设备 → 新增一台（随便填个 IP）→ 连接测试 / 备份

**没有真机时的预期行为**：备份会失败，界面显示 `unreachable` 或超时。
这不是 bug——它恰恰证明"失败路径 + 状态呈现 + 失败留痕"是通的（设备详情里应该能看到
一条失败的采集记录和错误原因）。

---

## 5. 真机联调

```powershell
# 5.1 先用探针确认这台设备的命令表（只发只读命令，不会改配置）
python scripts/spike_probe.py --vendor huawei --host <设备IP> --username <账号> `
    --out .spike/<设备名>
```

探针会试出：正确的关分页/关折行命令、取配置命令、是否弹交互确认，并做完整性校验。
把 `.spike/<设备名>/report.md` 的内容整理进 `docs/SPIKE_REPORT.md`。

然后：

1. 界面上建档（选厂商、绑凭据）→ 点「连接测试」→ **型号/软件版本/设备名会被自动补全**
2. 点「立即备份」→ 任务页看 SSE 实时进度与日志
3. 设备详情 → 查看配置（脱敏 / 明文切换）→ 下载
4. 改一次设备配置 → 再备份 → 对比版本，检查「重要变更」列表

> ⚠ **人工比对时的一个坑**：手工执行 `display current-configuration` 前，先把终端拉宽
> （或先执行 `screen-width 256`）。否则**手工结果反而会被折行**，比对出来会误导你以为
> 程序采错了。正确比法是程序输出与"宽终端下的手工输出"比对。

---

## 6. P1 验收清单

对照 `DEVELOPMENT_PLAN.md` §8 的 P1 行：

**交付内容**

- [ ] 驱动框架 + 会话层 + 三大驱动（华为/华三/锐捷）
- [ ] SQLite + 文件存储（配置正文内容寻址）
- [ ] 设备 CRUD（含凭据管理、CSV 批量导入）
- [ ] 单设备 / 批量手动备份（含 SSE 实时进度）
- [ ] 配置查看 / 下载（脱敏与明文的权限区分 + 审计）
- [ ] 近两版 diff（归一化后比对 + 重要变更识别）
- [ ] 登录鉴权（JWT + 四级角色）

**验收标准**

- [ ] 内网 50 台真实设备批量备份成功率 ≥ 95%
- [ ] 采集结果与人工执行结果一致（除已归一化的噪声行）

**辅助验证**

- [ ] `python scripts/preflight.py` 没有阻塞项（导入链 / 依赖 / 旧库 / 前端产物）
- [ ] `python scripts/selfcheck.py` 全绿（8 步）
- [ ] `python scripts/smoke.py` 全绿（P2 冒烟 21 项，见 `docs/P2_ACCEPTANCE.md` 步骤 1a）
- [ ] `python -m pytest -q` 全绿
- [ ] `npm run build` 成功，页面可登录并完成"建设备 → 备份 → 看 diff"闭环
- [ ] `docs/SPIKE_REPORT.md` 三家厂商各至少 2 个版本已填
  （当前只填了华为 VRP5 一台，**华三/锐捷仍缺真机实测**）

---

## 7. 失败特征对照表

| 现象 | 大概率原因 | 位置 |
|---|---|---|
| `ImportError: cannot import name 'X' from 'app.Y'` | 模块加了符号但没同步包级导出（`__init__.py`） | 对应的 `app/*/__init__.py` |
| `ModuleNotFoundError: No module named 'yaml'` | 依赖装到了另一个解释器 | 环境，不是代码（见第 0 节） |
| `NameError` / `AttributeError` at import | 类定义期求值的代码有问题 | 报错指向的模块 |
| SQLAlchemy `ArgumentError` / `InvalidRequestError` | mapper 配置问题（注解、relationship、索引名冲突） | `app/db/models.py` |
| `pydantic` 相关 `PydanticUserError` | schema 定义问题 | `app/schemas/*.py` |
| FastAPI 启动报参数/依赖错误 | 依赖注入写法问题 | `app/api/deps.py`、`app/api/v1/*.py` |
| `MissingGreenlet` | async 下触发了懒加载（关系没 eager load） | `app/services/*.py` 的查询 |
| `database is locked` | SQLite 并发写 | `app/services/backups.py`（已用锁串行化，若仍出现需调连接参数） |
| pytest：`tests/api/*` 全部 error，其它正常 | API 层导入或夹具建立失败（**采集层结果仍可信**） | `tests/support/env.py` 往上追 |
| `npm run build` 模板编译错误 | Vue 模板写法 / Element Plus API | 报错指向的 `.vue` |
| 界面能登录但列表空白 | 接口返回与前端字段不一致 | `web/src/views/*.vue` ↔ `app/schemas/*.py` |
| SSE 没有实时进度 | 反代缓冲未关（Nginx 需 `proxy_buffering off`）；或用 nginx 但没命中 stream location | `deploy/nginx.conf` |
| 备份报 `integrity_error` | 内容是残的（截断/折行/命令报错）——**内容仍会归档**，看 run 记录的 issue 列表 | 驱动 YAML 的命令表 |

---

## 8. 出问题时需要回传的最小信息

1. **执行的具体命令**
2. **完整输出**（失败时含结尾的错误段即可；pytest 只要失败清单）
3. `python -c "import sys; print(sys.version, sys.executable)"` 的输出
4. 是否连了真机；若是，厂商 + 型号 + 软件版本

有这些就能定位。**不需要**整理格式。
