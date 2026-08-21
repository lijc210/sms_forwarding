# 使用 Ubuntu 作为基础镜像
FROM debian:latest AS builder

# 更新软件包列表
RUN sed -i 's@deb.debian.org@mirror.sjtu.edu.cn@g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y vim wget curl net-tools build-essential && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 复制 uv 工具
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 进入开发阶段
FROM builder AS dev

WORKDIR /app

# 复制依赖文件
COPY uv.lock pyproject.toml ./

# 安装依赖（不安装项目）
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --no-install-project

# 复制源代码
COPY . .

# 同步项目
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev

# 启动命令
CMD [ ".venv/bin/python", "-u", "main.py" ]
