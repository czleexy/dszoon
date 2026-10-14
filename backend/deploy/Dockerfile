# 构建上下文是 backend/（不要用仓库根目录）：
#   docker build -f deploy/Dockerfile -t dszoon:0.1.0 .

# ---------------- 前端构建 ----------------
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json ./
# 用 install 而不是 ci：仓库里没有提交 package-lock.json 时 ci 会直接失败
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ---------------- 运行镜像 ----------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
# scripts/ 与 tests/ 一起带上：容器内可以直接跑 `python scripts/selfcheck.py`
# 验证这套部署是否正常（它会引用 tests/fixtures 里的样本回显），
# 对一个"装完不知道对不对"的私有化交付来说，这个自检入口比镜像大小重要。
COPY scripts ./scripts
COPY tests ./tests
# alembic.ini + migrations/ 必须带进镜像：否则容器里根本没有 `alembic` 可用，
# 想把 schema 交给 Alembic 管理（DSZOON_AUTO_CREATE_SCHEMA=false）就无从下手。
COPY alembic.ini ./
COPY migrations ./migrations
# 非可编辑安装：镜像里代码不需要再改，避免 editable install 的 .pth/finder 依赖
RUN pip install ".[web]"

# 前端产物：main.py 会自动挂载 web/dist（相对工作目录）
COPY --from=web /web/dist ./web/dist

ENV DSZOON_DATA_DIR=/data \
    DSZOON_DATABASE_URL=sqlite+aiosqlite:////data/dszoon.db
# 不以 root 运行：容器内进程只需要写 /data
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin dszoon \
 && mkdir -p /data \
 && chown -R dszoon:root /data
VOLUME ["/data"]

EXPOSE 8000
USER dszoon

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status==200 else 1)"

# 注意 --factory：create_app 是工厂函数，不是模块级 app 实例
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
