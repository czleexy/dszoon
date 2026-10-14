#!/usr/bin/env python3
"""P0 真机实测探针：把"这台设备到底该怎么采"一次问清楚。

用法（在 backend/ 目录下执行）::

    python scripts/spike_probe.py --vendor huawei --host 10.10.10.1 \
        --username admin --name HW-CORE-01 --out .spike/HW-CORE-01

    # 老设备：放开老旧算法 / 走 paramiko 兜底
    python scripts/spike_probe.py --vendor h3c --host 10.20.10.1 \
        --username admin --legacy --out .spike/H3C-OLD

    # 顺手把结果存成测试夹具（推荐：每测一台设备就攒一份夹具）
    python scripts/spike_probe.py ... --save-fixture --tag vrp8

它会做四件事：

1. 逐个试候选的「关分页 / 关折行」命令，报告哪条被设备接受、哪条被拒绝；
2. 逐个试取配置命令（含 ``| no-more`` 变体），报告哪条能拿到完整配置；
3. 对拿到的配置跑完整性校验，把问题（截断、折行、ANSI 残留）直接列出来；
4. 把原始会话记录、清洗结果、facts、以及一份可直接粘进
   ``docs/SPIKE_REPORT.md`` 的小结写到输出目录。

**这个脚本存在的意义**：计划里那张命令对照表是"应该是什么"，
这个脚本产出的是"实际上是什么"。先跑它，再改驱动。
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import getpass
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

# 允许 `python scripts/spike_probe.py` 直接运行（把 backend/ 放进 sys.path）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: 硬依赖。缺失时给一句能直接执行的修复命令，而不是丢 ModuleNotFoundError 堆栈——
#: 这类报错最容易让人以为"代码坏了"，实际上只是装到了另一个解释器里。
_REQUIRED_MODULES: tuple[tuple[str, str], ...] = (
    ("PyYAML", "yaml"),
    ("asyncssh", "asyncssh"),
)


def _check_dependencies() -> None:
    missing = [
        label for label, module in _REQUIRED_MODULES if importlib.util.find_spec(module) is None
    ]
    if not missing:
        return
    print("缺少依赖：" + "、".join(missing), file=sys.stderr)
    print(f"当前解释器：{sys.executable}", file=sys.stderr)
    print("", file=sys.stderr)
    print("修复步骤：", file=sys.stderr)
    print('  1) 在 backend/ 目录下执行：pip install -e ".[dev]"', file=sys.stderr)
    print("  2) 跑测试请用 `python -m pytest` 而不是裸 `pytest`——", file=sys.stderr)
    print("     裸 pytest 会用环境里的 pytest.exe，未必和 `python` 是同一个环境。", file=sys.stderr)
    print("  3) 确认解释器：python -c \"import sys; print(sys.executable)\"", file=sys.stderr)
    raise SystemExit(2)


_check_dependencies()

from app.collector import integrity, textutil  # noqa: E402
from app.collector.errors import CollectorError  # noqa: E402
from app.collector.registry import default_registry  # noqa: E402
from app.collector.session import SessionConfig, create_session  # noqa: E402

# --------------------------------------------------------------------------
# 候选命令：按"最可能生效"的顺序排列
# --------------------------------------------------------------------------
PAGING_CANDIDATES: dict[str, list[str]] = {
    "huawei": ["screen-length 0 temporary", "screen-length 0", "screen-length disable"],
    "h3c": ["screen-length disable", "screen-length 0 temporary", "screen-length 0"],
    "ruijie": ["terminal length 0", "terminal length 512"],
}

WIDTH_CANDIDATES: dict[str, list[str]] = {
    "huawei": ["screen-width 512", "screen-width 256"],
    "h3c": ["screen-width 512", "screen-width 256"],
    "ruijie": ["terminal width 512", "terminal width 256"],
}

#: 取配置命令的候选后缀（部分设备支持 | no-more 一次性关闭分页）
CONFIG_SUFFIXES = ["", " | no-more"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="网络设备配置采集探针（P0 实测工具）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--vendor", required=True, choices=default_registry.names())
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", default=None, help="不传则交互式输入")
    parser.add_argument("--enable-password", default=None)
    parser.add_argument("--name", default=None, help="台账里的设备名（用于收紧提示符正则）")
    parser.add_argument("--legacy", action="store_true", help="放开老旧 KEX/加密算法")
    parser.add_argument("--paramiko", action="store_true", help="改用 paramiko 兜底会话")
    parser.add_argument("--encoding", default=None, help="指定设备编码，如 gbk")
    parser.add_argument("--connect-timeout", type=float, default=15.0)
    parser.add_argument("--timeout", type=float, default=180.0, help="单条命令总超时")
    parser.add_argument("--idle", type=float, default=25.0, help="空闲超时")
    parser.add_argument("--out", default=".spike", help="输出目录")
    parser.add_argument("--save-fixture", action="store_true", help="同时保存为测试夹具")
    parser.add_argument("--tag", default=None, help="夹具子目录名，如 vrp8 / comware7")
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖已存在的夹具文件（默认拒绝，避免把假设备的样本冲掉）",
    )
    return parser.parse_args(argv)


async def run_probe(args: argparse.Namespace) -> int:
    driver = default_registry.get(args.vendor)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    password = args.password or getpass.getpass(f"{args.username}@{args.host} 密码: ")
    config = SessionConfig(
        host=args.host,
        port=args.port,
        username=args.username,
        password=password,
        enable_password=args.enable_password,
        connect_timeout=args.connect_timeout,
        command_timeout=args.timeout,
        idle_timeout=args.idle,
        legacy_algorithms=args.legacy,
        use_paramiko=args.paramiko,
        encoding=args.encoding,
    )

    report: dict[str, Any] = {
        "vendor": args.vendor,
        "host": args.host,
        "port": args.port,
        "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "session": config.sanitized(),
        "steps": [],
        "facts": {},
        "issues": [],
        "verdict": {},
    }

    session = create_session(config, driver.prompt_re(args.name), force_paramiko=args.paramiko)
    try:
        print(f"[*] 连接 {args.host}:{args.port} ...")
        await session.open()
        await session.begin()
        print("[+] 会话已建立")

        hostname = args.name
        if not hostname:
            line = session.last_received_line()
            hostname = driver.match_prompt(line or "") if line else None
            print(f"[*] 从提示符识别设备名：{hostname!r}（原始行 {line!r}）")
        session.prompt_re = driver.prompt_re(hostname)
        session.strict_prompt = hostname is not None
        session.confirm_rules = list(driver.confirm_rules())
        report["verdict"]["hostname"] = hostname

        # 1) 提权
        for step in driver.privilege_steps(enable_password=args.enable_password):
            outcome = await try_command(session, step.command, label=f"提权: {step.command}",
                                        secret=step.secret)
            if step.expect:
                outcome["note"] = f"期望提示符 {step.expect}"
            report["steps"].append(outcome)

        # 2) 版本
        version_output = ""
        if driver.version_cmd():
            step = await try_command(
                session, driver.version_cmd(), label=f"版本: {driver.version_cmd()}"
            )
            report["steps"].append(step)
            version_output = step.get("output", "") or ""
            (out_dir / "display_version.txt").write_text(version_output, encoding="utf-8")
        facts = driver.extract_facts(version_output, "")
        report["facts"] = facts
        print(f"[*] 版本识别：{facts or '（未识别出字段，需补充 facts 正则）'}")

        # 3) 关分页 / 关折行
        report["verdict"]["paging_command"] = await probe_candidates(
            session, report, PAGING_CANDIDATES.get(args.vendor, []), kind="paging"
        )
        report["verdict"]["width_command"] = await probe_candidates(
            session, report, WIDTH_CANDIDATES.get(args.vendor, []), kind="width"
        )

        # 4) 取配置（含 | no-more 变体）
        base_cmd = driver.running_config_cmd()
        best: dict[str, Any] | None = None
        for suffix in CONFIG_SUFFIXES:
            command = base_cmd + suffix
            step = await try_command(session, command, label=f"取配置: {command}")
            text = step.get("output") or ""
            issues = integrity.check_config(text, driver.config_spec()) if text else []
            step["issues"] = [dataclasses.asdict(issue) for issue in issues]
            step["line_count"] = textutil.line_count(text)
            step["bytes"] = len(text.encode("utf-8"))
            report["steps"].append(step)
            if text and not integrity.has_errors(issues):
                best = {"command": command, "text": text, "issues": issues}
                break
            if best is None and text:
                best = {"command": command, "text": text, "issues": issues}

        if best is None:
            print("[!] 未取到任何配置内容，请检查命令与权限")
            report["verdict"]["config_command"] = None
            report["verdict"]["integrity_ok"] = False
        else:
            text = best["text"]
            report["verdict"]["config_command"] = best["command"]
            report["verdict"]["integrity_ok"] = not integrity.has_errors(best["issues"])
            report["issues"] = [dataclasses.asdict(issue) for issue in best["issues"]]
            (out_dir / "running-config.cfg").write_text(text, encoding="utf-8", newline="\n")
            (out_dir / "running-config.normalized.cfg").write_text(
                driver.normalize(text), encoding="utf-8", newline="\n"
            )
            (out_dir / "running-config.redacted.cfg").write_text(
                driver.redact(text), encoding="utf-8", newline="\n"
            )
            report["facts"] = driver.extract_facts(version_output, text)
            print(
                f"[+] 配置采集成功：{best['command']} "
                f"({textutil.line_count(text)} 行 / {len(text.encode('utf-8'))} 字节)"
            )
            if best["issues"]:
                for issue in best["issues"]:
                    print(f"    {issue}")

            if args.save_fixture:
                save_fixture(driver, args, text, version_output)

        (out_dir / "session.log").write_text(
            session.transcript_text(), encoding="utf-8", newline="\n"
        )
    except CollectorError as exc:
        report["verdict"]["error"] = {"code": exc.code, "message": str(exc)}
        print(f"[x] 失败（{exc.code}）：{exc}", file=sys.stderr)
        return 2
    finally:
        await session.close()
        (out_dir / "facts.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        (out_dir / "report.md").write_text(render_report(report), encoding="utf-8", newline="\n")
        print(f"[*] 结果已写入 {out_dir}（report.md / session.log / facts.json）")

    return 0


async def try_command(
    session,
    command: str,
    *,
    label: str,
    secret: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    record: dict[str, Any] = {"label": label, "command": command if not secret else "******"}
    confirmations_before = getattr(session, "confirmation_count", 0)
    try:
        output = await session.send_command(command, secret=secret, timeout=timeout)
        record["output"] = output
        issues = integrity.check_command_output(output)
        record["ok"] = not issues
        if issues:
            record["error"] = issues[0].message
    except CollectorError as exc:
        record["ok"] = False
        record["error"] = f"{exc.code}: {exc}"
        await session.resync()
    finally:
        record["confirmations"] = (
            getattr(session, "confirmation_count", 0) - confirmations_before
        )
    record["seconds"] = round(time.perf_counter() - started, 2)
    mark = "OK  " if record.get("ok") else "FAIL"
    print(f"    [{mark}] {label}  ({record['seconds']}s)"
          + (f"  -> {record.get('error')}" if record.get("error") else ""))
    if record["confirmations"]:
        print(f"           （自动回答了 {record['confirmations']} 次交互确认，如 [Y/N]）")
    return record


async def probe_candidates(session, report: dict[str, Any], candidates: list[str], *, kind: str) -> str | None:
    accepted: str | None = None
    for command in candidates:
        record = await try_command(session, command, label=f"{kind}: {command}")
        record["kind"] = kind
        report["steps"].append(record)
        if record.get("ok") and accepted is None:
            accepted = command
            break  # 第一个被接受的即可，后续候选不再试探（避免改变设备状态）
    return accepted


def save_fixture(driver, args: argparse.Namespace, config_text: str, version_text: str) -> None:
    """把真实回显存成测试夹具。

    ★ 这里有两个坑，都在下面显式挡住了：

    1. **内置合成夹具会被冲掉**。`tests/fixtures/<vendor>/<tag>/` 里的文件被
       `tests/mock_device/scenarios.yaml` 引用，mock 就是拿它们当"设备回显"的。
       直接覆盖会让一批测试的断言（型号、中文描述、行数）当场失效——
       而报错会出现在测试里，跟"我刚跑了个探针"看起来毫无关系。
    2. **夹具会进版本库**。真实配置里可能含密文口令、内网地址、客户信息。
    """
    tag = args.tag or "unknown"
    root = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / driver.name / tag
    names = {
        "huawei": ("display_current-configuration.txt", "display_version.txt"),
        "h3c": ("display_current-configuration.txt", "display_version.txt"),
        "ruijie": ("show_running-config.txt", "show_version.txt"),
    }
    config_name, version_name = names.get(driver.name, ("running-config.txt", "version.txt"))
    targets = [root / config_name] + ([root / version_name] if version_text else [])
    existing = [path for path in targets if path.exists()]

    if existing and not args.force:
        print(f"[!] 夹具已存在，未覆盖：{', '.join(p.name for p in existing)}")
        print("    这些文件被 tests/mock_device/scenarios.yaml 引用，覆盖会让相关断言失效。")
        print("    确认要替换请加 --force，或换一个 --tag（例如 --tag vrp8-real）。")
        return

    root.mkdir(parents=True, exist_ok=True)
    (root / config_name).write_text(config_text, encoding="utf-8", newline="\n")
    if version_text:
        (root / version_name).write_text(version_text, encoding="utf-8", newline="\n")
    print(f"[*] 夹具已保存到 {root}")
    print("[!] 夹具是真实设备配置，可能含密文口令与内网信息，入库前请确认符合数据管理要求。")


def render_report(report: dict[str, Any]) -> str:
    verdict = report.get("verdict", {})
    lines = [
        f"# 实测小结：{report['vendor']} @ {report['host']}（{report['probed_at']}）",
        "",
        f"- 识别设备名：`{verdict.get('hostname')}`",
        f"- 关分页命令：`{verdict.get('paging_command')}`",
        f"- 关折行命令：`{verdict.get('width_command')}`",
        f"- 取配置命令：`{verdict.get('config_command')}`",
        f"- 完整性校验：{'通过' if verdict.get('integrity_ok') else '**未通过，需人工确认**'}",
        f"- facts：`{json.dumps(report.get('facts', {}), ensure_ascii=False)}`",
        "",
        "## 命令试探结果",
        "",
        "| 命令 | 结果 | 耗时(s) | 备注 |",
        "|---|---|---|---|",
    ]
    for step in report.get("steps", []):
        note = step.get("error") or step.get("note") or ""
        if step.get("confirmations"):
            note = f"{note} 自动回答确认 {step['confirmations']} 次".strip()
        lines.append(
            f"| `{step.get('label')}` | {'OK' if step.get('ok') else 'FAIL'} "
            f"| {step.get('seconds')} | {note} |"
        )
    if report.get("issues"):
        lines += ["", "## 完整性问题", ""]
        for issue in report["issues"]:
            lines.append(f"- [{issue['severity']}] `{issue['code']}`：{issue['message']}")
    if verdict.get("error"):
        lines += ["", f"## 失败：`{verdict['error']['code']}` {verdict['error']['message']}"]
    lines += [
        "",
        "## 待人工确认",
        "",
        "- [ ] 采集结果与在设备上手工执行取配置命令的结果**逐字节比对**是否一致",
        "- [ ] 是否存在长度异常的长行（折行嫌疑）",
        "- [ ] 提权方式是否正确（是否需要 enable/super 密码）",
        "- [ ] 单次采集耗时是否可接受（大配置设备可能 30s+）",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run_probe(args))
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
