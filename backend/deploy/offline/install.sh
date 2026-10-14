#!/usr/bin/env bash
# 内网目标机器上执行：离线安装 dszoon。
#
# 这个文件是**真实文件**（不再由打包脚本拼 heredoc），make_bundle.sh 与
# make_bundle.ps1 都复制同一份——两处逻辑分叉过一次就够难受了。
#
# 用法：
#   tar -xzf dszoon-offline-<日期>.tar.gz && cd dszoon-offline-<日期> && bash install.sh
#
# 可选环境变量：PY_BIN（默认 python3，指向目标机的解释器）
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

PY_BIN="${PY_BIN:-python3}"

echo "==> 核对打包环境"
# wheel 与**平台 + Python 小版本**绑定。对不上时 pip 会报"找不到匹配的版本"，
# 那句话不会告诉你"包是为别的平台打的"，所以先在这里把它说清楚。
if [ -f PACKAGING.txt ]; then
  sed 's/^/    /' PACKAGING.txt
  packed_py="$(sed -n 's/^python=//p' PACKAGING.txt)"
  here_py="$("${PY_BIN}" -c 'import platform; print(platform.python_version())' 2>/dev/null || echo "?")"
  if [ -n "${packed_py}" ] && [ "${packed_py}" != "${here_py}" ]; then
    echo "    ! 打包机 Python ${packed_py} ≠ 本机 ${here_py}"
    echo "      若下面的 pip 报找不到匹配的版本，这就是原因（回到打包机重打）"
  fi
else
  echo "    （包里没有 PACKAGING.txt，跳过核对）"
fi

echo "==> 创建虚拟环境"
"${PY_BIN}" -m venv venv

echo "==> 离线安装依赖（只用本地 wheels，不联网）"
./venv/bin/pip install --no-index --find-links wheels -e .

echo "==> 放置前端产物（后端会自动挂载 web/dist）"
mkdir -p web
rm -rf web/dist
cp -r web-dist web/dist

echo "==> 生成配置模板"
if [ ! -f .env ]; then
  cat > .env <<EOF
DSZOON_DATABASE_URL=sqlite+aiosqlite:///./var/dszoon.db
DSZOON_DATA_DIR=./var
# 下面两个必须自己生成并长期保存：改掉 secret_key 会导致设备密码要重录
DSZOON_SECRET_KEY=$(./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
DSZOON_JWT_SECRET=$(./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))")
# 初始管理员口令（只在首次建库时生效；留空 = 随机生成并在启动日志里打印一次）
DSZOON_ADMIN_PASSWORD=
EOF
  chmod 600 .env
  echo "    已生成 .env（含随机密钥）；数据库连接与管理员口令按需修改"
fi

echo "==> 装完先自检（会调用 tests/ 里的假设备夹具，确认这套部署是好的）"
echo "    ./venv/bin/python scripts/selfcheck.py"

echo "==> 完成。启动："
echo "    ./venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000"
