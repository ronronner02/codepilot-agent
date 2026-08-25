# 后端镜像。
#
# 两阶段：builder 装依赖（含需要编译的 tree-sitter 与 chromadb），运行阶段只带装好的包。
# 这样运行镜像不含 gcc 与头文件——那些只在构建期需要，留着会让镜像大一倍并扩大攻击面。

FROM python:3.13-slim AS builder

# tree-sitter 与 chromadb 的部分依赖需要编译。这些包只在 builder 阶段存在。
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 从 pyproject.toml 装，不在这里另抄一份依赖清单。
#
# 抄一份的后果是两处要同步：往 pyproject 加依赖却忘了改 Dockerfile，容器会在运行时
# 报 ImportError——而那时离「加了个依赖」这个动作已经很远。
#
# hatchling 需要包本身在场才能构建，所以连 backend 一起复制。代价是改任何后端代码都会
# 让这一层失效并重装依赖；换来的是依赖只有一个来源。
COPY pyproject.toml ./
COPY backend ./backend
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.13-slim

# **git 是运行期依赖，不是构建期的。** 后端要 clone 被分析的仓库（U2），
# 没有它每次分析都会在克隆那一步失败，而错误信息会指向 git 不存在——离「为什么分析不了」
# 这个现象很远。
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY backend ./backend
COPY pyproject.toml ./

# 工作区放在卷挂载点上。
#
# 必须与 U9 的缓存落盘路径一致（settings.repos_dir / index_dir 都由 workspace_root 派生），
# 否则容器重启后索引缓存失效、每次都重新付 embedding 成本——那正是 R4 要避免的。
ENV WORKSPACE_ROOT=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN mkdir -p /data && useradd --create-home --uid 10001 codepilot \
    && chown -R codepilot:codepilot /data /app
USER codepilot

EXPOSE 8000

# 健康检查打 /api/health。它不碰 LLM 也不碰向量库，所以不会因外部服务不可用而误报不健康。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# 单 worker：分析任务的状态在进程内存里（TaskRegistry），多 worker 会让「提交到 A、
# 拉进度从 B」拿不到任务。要横向扩展得先把任务状态外置，那不在本期范围内。
CMD ["python", "-m", "uvicorn", "backend.main:get_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
