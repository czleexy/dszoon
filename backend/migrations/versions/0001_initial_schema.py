"""初始 schema（P0~P2 的全部 12 张表）

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00

这个基线版本**直接用模型的 metadata 建表**，而不是手抄一遍 ``create_table``：

* 手抄 12 张表 + 二十多个索引，漏一处（少个索引、索引名不一致、外键的
  ``ondelete`` 写错）都不会在功能测试里报错。它只会在某天清理数据时才暴露——
  那是"删了分组，设备跟着没了"级别的事故；而索引写漏只会让 1000 台的列表页
  在某天突然变慢，同样很难归因。
* 本版本是**基线**：项目还没有任何已部署实例，"基线 == 当前模型"是安全且
  最不容易出错的表述。

★ 一旦有实例升级过这个版本，后续 schema 变化**必须**用
``alembic revision --autogenerate -m "..."`` 生成独立迁移，不要再改这个文件——
否则已经升级过的库不会得到新表/新列。
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def _metadata():
    # 在函数内导入：alembic 载入这个模块时会先执行顶层代码，而 app.db 的导入
    # 需要 app 包可导入（prepend_sys_path=. 已保证），放函数里可以让报错更清晰
    from app.db import models  # noqa: F401  导入以把全部模型注册进 Base.metadata
    from app.db.base import Base

    return Base.metadata


def upgrade() -> None:
    _metadata().create_all(bind=op.get_bind())


def downgrade() -> None:
    _metadata().drop_all(bind=op.get_bind())
