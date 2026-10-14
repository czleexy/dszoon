"""Pydantic DTO（对外契约）。

约定：

* 时间统一输出为 UTC ISO8601 并带 ``Z``（库里存朴素 UTC，见 ``app.db.base``）。
* **凭据类字段永不出现在响应模型里**——只有 ``has_password`` 之类的布尔标记。
* 设备凭据只在构造 ``DeviceTarget`` 的那一刻解密，不进任何 schema。
"""

from __future__ import annotations
