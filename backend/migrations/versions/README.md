# 迁移脚本目录。
#
# 第一个迁移用这条命令生成（它会对比 ORM 模型与**目标库**）：
#   cd backend
#   alembic revision --autogenerate -m "init"
#   alembic upgrade head
#
# ★ 生成第一个迁移前，目标库必须是**空的**。
#   如果先跑过一次服务（create_all 已建表），autogenerate 会认为"没有差异"，
#   生成一个空迁移 —— 部署时 upgrade head 什么都不做，服务起来后表就不见了。
#
# 生成后**务必看一眼脚本内容**：
#   * autogenerate 不认识表/列重命名，会当成"删除旧表 + 新建新表"，数据直接没了；
#   * 它不生成任何数据迁移（比如给已有行补默认值），需要手工加 op.execute。
