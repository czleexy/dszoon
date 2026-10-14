"""首跑前的 10 秒体检（**只读**：不建库、不写文件、不联网）。

它只回答四个最容易让"第一次运行"卡住、而且从报错里看不出来的问题：

1. **导入链能不能起来**——缺依赖或包级出口漏一个符号，会让 pytest 在收集阶段
   整体报错（看起来像"改坏了核心代码"）；
2. **关键依赖是否齐全**——尤其 P2 新增的 APScheduler；
3. **工作目录里的库是不是旧 schema**——``create_all`` 只补建缺失的表、**不给已有表
   加列**，沿用 P1 老库会得到"启动正常、设备页整页 500"；
4. **前端产物是不是比源码旧**——改了页面忘了 ``npm run build``，界面就还是老样子。

这四件事都是"知道以后一秒钟能解决，不知道要查半天"的类型，所以单独做成一条命令。

用法::

    cd backend
    python scripts/preflight.py

退出码 0 = 可以直接跑 pytest；非 0 = 先按输出里的提示处理。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

#: P2 才有的表名（用来判断工作目录里的库是不是 P1 老库）
P2_MARKERS = (b"device_groups", b"notification_channels", b"retention_policy")

#: 抓 `export const x` / `export async function x` 这类具名导出
_JS_EXPORT_RE = re.compile(
    r"^export\s+(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", re.M
)
#: 抓 `export { a, b }` 写法（现在没有，留着防止以后改写法时误报）
_JS_EXPORT_LIST_RE = re.compile(r"^export\s*\{([^}]*)\}", re.M | re.S)
#: 抓 `import { a, b as c } from '...endpoints'`，含跨行的花括号
_JS_IMPORT_RE = re.compile(r"import\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]*endpoints)['\"]", re.S)

#: 阻塞项（会让 pytest 或应用起不来）
_blockers: list[str] = []
#: 建议项（不影响 pytest，但会影响界面验证）
_suggestions: list[str] = []


def ok(message: str) -> None:
    print(f"  {GREEN}✓{RESET} {message}")


def warn(message: str, action: str | None = None) -> None:
    print(f"  {YELLOW}!{RESET} {message}")
    if action:
        _suggestions.append(action)


def bad(message: str, action: str | None = None) -> None:
    print(f"  {RED}✗{RESET} {message}")
    if action:
        _blockers.append(action)


def check_imports() -> None:
    print("[1/4] 导入链")
    modules = (
        "app.main",
        "app.api.v1.ops",
        "app.services.scheduler",
        "app.services.notifications",
        "app.services.retention",
        "app.services.schedules",
        "app.services.backups",
        "app.collector",
    )
    for name in modules:
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 - 这里就是要展示真实原因
            action = (
                'pip install -e ".[dev,web]"'
                if isinstance(exc, ModuleNotFoundError)
                else None
            )
            bad(f"{name} 导入失败：{type(exc).__name__}: {exc}", action)
            return
    ok(f"导入链正常（{len(modules)} 个关键模块）")


def check_dependencies() -> None:
    print("[2/4] 依赖")
    required = (
        ("asyncssh", "采集层主会话"),
        ("paramiko", "采集层回退"),
        ("yaml", "驱动 YAML"),
        ("fastapi", "Web 框架"),
        ("sqlalchemy", "ORM"),
        ("jwt", "令牌"),
        ("cryptography", "凭据加密"),
        ("apscheduler", "cron 解析（P2 新增）"),
    )
    missing = [
        f"{label}（{module}）" for module, label in required if importlib.util.find_spec(module) is None
    ]
    if missing:
        bad("缺少依赖：" + "、".join(missing), 'pip install -e ".[dev,web]"')
    else:
        ok(f"依赖齐全（{len(required)} 个）")

    missing_dev = [name for name in ("pytest", "httpx") if importlib.util.find_spec(name) is None]
    if missing_dev:
        warn(
            f"开发依赖缺失：{'、'.join(missing_dev)}（pytest/端到端测试需要）",
            action='pip install -e ".[dev,web]"',
        )


def check_database() -> None:
    print("[3/4] 工作目录里的数据库")
    default = ROOT / "var" / "dszoon.db"
    if not default.exists():
        ok("没有已存在的 SQLite 库：首次启动会按当前模型建表")
        return
    try:
        raw = default.read_bytes()
    except OSError as exc:  # pragma: no cover - 权限/占用
        warn(f"读不了 {default.name}（{exc}），跳过检查")
        return
    if all(marker in raw for marker in P2_MARKERS):
        ok("库已包含 P2 的表，可以直接用")
        return
    bad(
        f"{default.name} 是旧 schema 的库（没有 P2 的表）",
        action=f"Remove-Item {default}  # 然后重启应用；★ 保留 var\\.secrets.json",
    )
    print(
        "      说明：create_all 只补建缺失的表、不会给已有表加列，直接启动会缺\n"
        "      devices.group_id / backup_tasks.schedule_id（表现为设备页整页 500）"
    )


def check_frontend_imports() -> None:
    """前端具名导入 vs endpoints.js 的实际导出。

    为什么值得单独查：``src/api/endpoints.js`` 里一句 ``export`` 如果被行注释吞掉
    （编辑时丢了行尾换行，变成 ``// ---- 标题 ----export const x = ...`` 这种），
    文件本身**语法仍然合法**——只是那个导出不存在了，而下一行的 ``http.get(...)``
    会变成一句游离的表达式。pytest 完全看不到这一层（它不碰 JS），
    只有 ``npm run build`` 会报 ``"x" is not exported by ...``，而且一次只报一个，
    修一个再跑一轮。这里一次把整张表对完。
    """
    src = ROOT / "web" / "src"
    api = src / "api" / "endpoints.js"
    if not api.is_file():
        return
    try:
        text = api.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 权限/占用
        warn(f"读不了 {api.name}（{exc}），跳过前端导入检查")
        return

    exports = set(_JS_EXPORT_RE.findall(text))
    for group in _JS_EXPORT_LIST_RE.findall(text):
        exports.update(part.split(" as ")[-1].strip() for part in group.split(","))

    missing: list[str] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix not in {".js", ".vue", ".ts"}:
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover
            continue
        for names, _source in _JS_IMPORT_RE.findall(body):
            for entry in names.split(","):
                name = re.split(r"\s+as\s+", entry.strip())[0].strip()
                if name and name not in exports:
                    missing.append(f"{path.relative_to(src)} 导入了不存在的 {name}")

    if missing:
        bad(
            "前端导入了不存在的接口函数（npm run build 会失败）：" + "；".join(missing),
            action="检查 web/src/api/endpoints.js：export 是否被行注释吃掉",
        )
    else:
        ok(f"前端具名导入与 endpoints.js 一致（{len(exports)} 个导出）")


def check_frontend() -> None:
    print("[4/4] 前端产物")
    check_frontend_imports()
    dist = ROOT / "web" / "dist"
    source = ROOT / "web" / "src"
    if not dist.is_dir():
        warn(
            "没有 web/dist：界面打不开（只影响前端，API 与 /docs 正常）",
            action="cd web; npm install; npm run build",
        )
        return
    if not source.is_dir():
        ok("web/dist 存在（镜像里通常只有它，无法比较新旧）")
        return
    index = dist / "index.html"
    if not index.is_file():
        warn("web/dist 里没有 index.html，产物可能不完整", action="cd web; npm run build")
        return
    built = index.stat().st_mtime
    newest = max(
        (path.stat().st_mtime for path in source.rglob("*") if path.is_file()), default=0.0
    )
    if newest > built:
        warn(
            "web/dist 比 web/src 旧：界面会停留在旧版本（新页面/新菜单看不到）",
            action="cd web; npm run build",
        )
    else:
        ok("web/dist 不比源码旧")


def main() -> int:
    print(f"dszoon 首跑前体检（{ROOT}）\n")
    check_imports()
    check_dependencies()
    check_database()
    check_frontend()

    print()
    if _suggestions:
        print(f"{YELLOW}建议（不阻塞 pytest，但会影响界面验证）：{RESET}")
        for item in dict.fromkeys(_suggestions):
            print(f"  - {item}")
    if _blockers:
        print(f"{RED}先处理这几件事：{RESET}")
        for item in dict.fromkeys(_blockers):
            print(f"  - {item}")
        return 1
    print(f"{GREEN}没有阻塞项。接下来：python -m pytest -q{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
