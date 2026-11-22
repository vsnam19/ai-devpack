# eval/Dockerfile - Isolated execution container for ai-devpack evaluations.
#
# Builds a self-contained environment with Node.js LTS, the Claude Code CLI
# pinned via build argument, a C++17 toolchain (g++, cmake), Python 3, and git.
# Runs as a non-root user (UID/GID 1000) with no host filesystem mounts except
# the per-trial task workspace and a read-only temporary credential directory.

FROM node:20-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.330

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        cmake \
        coreutils \
        findutils \
        g++ \
        gawk \
        git \
        grep \
        make \
        patch \
        python3 \
        sed \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

RUN mkdir -p /workspace/task /workspace/home /workspace/cfg \
    && chown -R 1000:1000 /workspace

ENV HOME=/workspace/home
ENV CLAUDE_CONFIG_DIR=/workspace/cfg
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

USER 1000:1000
WORKDIR /workspace/task

CMD ["claude", "--version"]
