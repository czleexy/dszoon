#!/usr/bin/env bash
# 在内网隔离环境部署时，用这台"有外网的机器"把依赖与前端产物打包好。
#
#   用法（在 backend/ 目录下执行）：
#     bash deploy/offline/make_bundle.sh
#   产物：deploy/offline/dist/dszoon-offline-<日期>.tar.gz
#
# ★ wheel 与**平台 + Python 小版本**绑定：包里那堆轮子只能装到与打包机同 OS、
#   同架构、同 Python 小版本的目标机上。cryptography / asyncpg / pydantic-core
#   都是编译扩展，平台不对会在目标机的 pip install 阶段直接失败。
#   跨平台（Windows 打包、Linux 内网部署）不要用这个脚本：要么在 Linux 容器里
#   跑它，要么直接走 README §4 的 Docker 镜像包。
#
# 目标机器上的安装步骤见 deploy/offline/README.md（会一起打进包里）。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
STAMP="$(date +%Y%m%d)"
BUNDLE="${ROOT}/deploy/offline/dist/dszoon-offline-${STAMP}"

echo "==> 工作目录：${ROOT}"
rm -rf "${BUNDLE}"
mkdir -p "${BUNDLE}/wheels" "${BUNDLE}/web-dist" "${BUNDLE}/deploy"

echo "==> 1/5 下载 Python 依赖（含 web extras）"
# 目标机器的 Python 版本要与这里一致，否则可能下到不匹配的 wheel
python -m pip download -d "${BUNDLE}/wheels" ".[web]"
# ★ 构建期依赖也要带上：Python 3.12 起 venv 默认不再自带 setuptools，
#   离线机器上 `pip install -e . --no-index` 会因为找不到 setuptools 直接失败。
python -m pip download -d "${BUNDLE}/wheels" setuptools wheel

echo "==> 2/5 构建前端产物"
if [ ! -d "${ROOT}/web/node_modules" ]; then
  (cd "${ROOT}/web" && npm install --no-audit --no-fund)
fi
(cd "${ROOT}/web" && npm run build)
cp -r "${ROOT}/web/dist/." "${BUNDLE}/web-dist/"

echo "==> 3/5 复制源码与测试夹具"
cp -r "${ROOT}/app" "${BUNDLE}/app"
cp -r "${ROOT}/scripts" "${BUNDLE}/scripts"
# ★ tests/ 必须带上：scripts/selfcheck.py 会 import tests.support.fake_device
#   并读 tests/fixtures 下的样本回显。漏了它，内网现场的"装完自检"会直接崩，
#   而那时你已经查不到原因。（Dockerfile 里同样带了 tests/，理由相同。）
cp -r "${ROOT}/tests" "${BUNDLE}/tests"
cp "${ROOT}/pyproject.toml" "${BUNDLE}/"
# alembic.ini + migrations/：离线包也要能在现场把 schema 交给 Alembic 管理
cp "${ROOT}/alembic.ini" "${BUNDLE}/"
cp -r "${ROOT}/migrations" "${BUNDLE}/migrations"

echo "==> 4/5 复制部署文件"
# nginx.conf 也要带上：docker-compose.yml 的 with-nginx profile 挂着它，
# 包里没有的话那个 profile 一启动就报错。
cp "${ROOT}/deploy/Dockerfile" "${ROOT}/deploy/docker-compose.yml" \
   "${ROOT}/deploy/.env.example" "${ROOT}/deploy/nginx.conf" "${BUNDLE}/deploy/"
cp -r "${ROOT}/deploy/systemd" "${BUNDLE}/deploy/"
cp "${ROOT}/deploy/offline/README.md" "${BUNDLE}/deploy/"
# install.sh 用仓库里的真实文件，不在这里拼 heredoc：bash 与 PowerShell 两个
# 打包器复制同一份，避免两处逻辑悄悄分叉。
cp "${ROOT}/deploy/offline/install.sh" "${BUNDLE}/install.sh"
chmod +x "${BUNDLE}/install.sh"

echo "==> 5/5 记录打包环境并打包"
# 装依赖失败时第一个要核对的就是"平台/Python 是否与打包机一致"，
# 所以把打包环境写进包里，目标机的 install.sh 会拿它对比并给出提示。
{
  echo "packed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "os=$(uname -s)"
  echo "arch=$(uname -m)"
  echo "python=$(python -c 'import platform; print(platform.python_version())')"
} > "${BUNDLE}/PACKAGING.txt"
cat "${BUNDLE}/PACKAGING.txt"

(cd "$(dirname "${BUNDLE}")" && tar -czf "$(basename "${BUNDLE}").tar.gz" "$(basename "${BUNDLE}")")

echo
echo "完成：$(dirname "${BUNDLE}")/$(basename "${BUNDLE}").tar.gz"
echo "目标机器上：tar -xzf 包名.tar.gz && cd 包名 && bash install.sh"
