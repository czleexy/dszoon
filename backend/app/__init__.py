"""dszoon 网络设备配置备份系统 —— 采集端骨架（P0）。

当前实现范围（只读）：
    - 设备会话建立（SSH，paramiko 兜底；Telnet 预留）
    - 终端流清洗（ANSI / 退格 / 分页 / 编码 / 回显）
    - 厂商驱动（华为 VRP、华三 Comware、锐捷 RGOS），声明式 YAML 可扩展
    - 采集引擎（并发、限流、重试、可达性预检）
    - 完整性校验（防止"备份成功但内容是残的"）
    - 内容寻址归档

明确不在 P0 范围：
    - 配置下发 / 回滚（接口已预留，见 ``collector.drivers.base.VendorDriver.deploy``）
    - Web API / 数据库 / 前端（P1 起）
"""

__version__ = "0.1.0"
