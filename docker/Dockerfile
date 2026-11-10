# 严格隔离模式用的镜像（bench.py setup 在 isolation=docker 时自动构建）。镜像里没有任何测试相关的文件。
FROM node:22-bookworm-slim
ARG DSH_VERSION=latest
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages pytest openpyxl
RUN npm i -g @deepseek-ai/dsh@${DSH_VERSION} && npm cache clean --force
# JS 题在断网容器里也能自己跑 jest：预装一份通用 jest/babel，工作目录里的 node_modules 链接指向这里
COPY nodekit-package.json /opt/nodekit/package.json
RUN cd /opt/nodekit && npm install --no-audit --no-fund && npm cache clean --force && chmod -R a+rX /opt/nodekit
