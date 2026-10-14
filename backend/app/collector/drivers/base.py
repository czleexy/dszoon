"""厂商驱动抽象。

一个厂商驱动回答四个问题：

1. **怎么认提示符**——登录态到了没有、命令跑完了没有、提示符变化了没有；
2. **发什么命令**——关分页、关折行、提权、取配置；
3. **怎么判定内容可信**——完整性校验规则（见 :mod:`..integrity`）；
4. **怎么归一化与脱敏**——供 diff 比对和对外展示。

:meth:`VendorDriver.deploy` 是**为配置下发预留的接口**：v1.0 全部实现都会抛
:class:`~..errors.DeployNotSupported`，但签名先定下来，避免将来为了加下发能力
而重构采集引擎。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Pattern, Sequence

from .. import integrity
from ..errors import DeployNotSupported
from ..session import ConfirmRule, InteractiveSession

__all__ = ["ConfirmRule", "DriverInfo", "PrivilegeStep", "VendorDriver"]


@dataclass(frozen=True, slots=True)
class PrivilegeStep:
    """提权流程中的一步。

    :param expect: 期望看到的提示符正则；``None`` 表示主提示符（即这一步是普通命令）
    :param secret: 该步输入是密码，不得进入任何日志
    :param optional: 超时后允许"会话没坏，继续"——设备没设 enable 密码时
        ``enable`` 会直接成功而不弹 ``Password:``，这是必须容忍的正常情况
    """

    command: str
    expect: str | None = None
    secret: bool = False
    timeout: float | None = None
    optional: bool = False


@dataclass(frozen=True, slots=True)
class DriverInfo:
    name: str
    display_name: str
    aliases: tuple[str, ...] = ()
    description: str = ""


class VendorDriver(ABC):
    """厂商驱动基类。"""

    name: ClassVar[str] = ""
    display_name: ClassVar[str] = ""
    aliases: ClassVar[tuple[str, ...]] = ()
    description: ClassVar[str] = ""
    #: v1.0 全部为 False；置 True 表示该驱动实现了配置下发
    supports_deploy: ClassVar[bool] = False

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    @property
    def info(self) -> DriverInfo:
        return DriverInfo(self.name, self.display_name, tuple(self.aliases), self.description)

    def matches(self, vendor: str) -> bool:
        value = (vendor or "").strip().lower()
        return value == self.name.lower() or value in {
            alias.lower() for alias in self.aliases
        }

    # ------------------------------------------------------------------
    # 提示符
    # ------------------------------------------------------------------
    @abstractmethod
    def prompt_re(self, hostname: str | None = None) -> Pattern[str]:
        """返回提示符正则。

        :param hostname: 已知设备名时传入，实现应生成**更严格**的正则
            （把通配的设备名换成字面量）。这不是优化，而是正确性保障：
            泛化的 ``^<\\w+>$`` 有可能被配置正文里的某一行命中，导致采集
            在配置中途就"以为命令跑完了"，采到一半的内容。
        """

    def match_prompt(self, line: str) -> str | None:
        """从一行文本识别提示符并返回设备名（用于反查真实 hostname）。"""
        return None

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    @abstractmethod
    def running_config_cmd(self) -> str:
        """取当前运行配置的命令。"""

    def startup_config_cmd(self) -> str | None:
        """取下次启动配置的命令（可选）。"""
        return None

    def version_cmd(self) -> str | None:
        """取版本信息的命令（可选，用于识别型号/版本，从而走正确的命令分支）。"""
        return None

    def session_setup_cmds(self) -> Sequence[str]:
        """登录后必须执行的会话预处理命令（关分页、关折行等）。

        顺序会被保留——例如必须先 ``screen-length 0 temporary`` 再取配置。
        """
        return ()

    def privilege_steps(self, *, enable_password: str | None = None) -> Sequence[PrivilegeStep]:
        """提权步骤。默认无需提权。"""
        return ()

    def confirm_rules(self) -> Sequence[ConfirmRule]:
        """需要自动回答的交互确认（正则, 回复）。

        只应用于**我们自己主动发送的、语义明确的**命令——例如关分页、设终端宽度
        时弹出的 "Continue? [Y/N]:"。这类确认不回答，会话就会一直干等，
        而报错只会是含糊的"会话空闲 N 秒无数据"。

        ⚠ 配置下发（``deploy``）绝不能复用这个机制自动放行：那是变更操作，
        必须经过人工审批与 diff 确认。
        """
        return ()

    # ------------------------------------------------------------------
    # 后处理
    # ------------------------------------------------------------------
    def config_spec(self) -> integrity.ConfigSpec:
        """该厂商配置的完整性判定标准。"""
        return integrity.ConfigSpec()

    def normalize(self, text: str) -> str:
        """归一化：去掉易变噪声（时间戳、运行时长、字节数统计），供 diff 比对。

        **不得修改配置语义**——归一化后的文本只用于比较，归档原文必须是原始清洗结果。
        """
        return text

    def redact(self, text: str) -> str:
        """脱敏：掩掉口令、SNMP community、密钥等敏感串。"""
        return text

    def extract_facts(self, version_output: str, config_text: str) -> dict[str, str]:
        """从版本输出与配置正文里抽取结构化字段（型号/软件版本/序列号/hostname）。"""
        return {}

    def extract_hostname(self, config_text: str) -> str | None:
        return None

    # ------------------------------------------------------------------
    # 预留：配置下发 / 回滚（v1.0 未实现）
    # ------------------------------------------------------------------
    async def deploy(
        self,
        session: InteractiveSession,
        config_text: str,
        *,
        dry_run: bool = True,
        save: bool = True,
    ) -> str:
        """把配置下发到设备。**v1.0 全部驱动都会抛异常。**

        未来的实现必须满足（写在签名里是为了提前约束）：

        * ``dry_run=True`` 时只做回显比对，不下发，用于生成"将要发生什么"的预览；
        * 下发前必须由调用方完成审批与 diff 确认，驱动本身不做权限判断；
        * 全程写审计日志，包括下发的完整配置文本与设备逐行回显；
        * 任一行报错立即中止，不"继续下发剩余部分"——半套配置比不配置更危险。
        """
        raise DeployNotSupported(
            f"驱动 {self.name!r} 未实现配置下发（v1.0 只读）；"
            "该能力计划在 P4 阶段以审批流 + diff 预览的方式引入"
        )
