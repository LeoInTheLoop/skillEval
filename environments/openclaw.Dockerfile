# OpenClaw 评测镜像：容器里跑 agent loop，宿主机只留编排。
#
# 版本写死不用 latest —— 镜像 digest 进 config_hash，浮动 tag 会让「同一个
# config_hash 跑出不同结果」。升级 OpenClaw 就是换一个 digest，是一次显式变更。
#
# build（digest 就是 image ID，本地镜像没有 registry digest）：
#   docker build -f environments/openclaw.Dockerfile -t skilleval-openclaw .
#   docker image inspect skilleval-openclaw --format '{{.Id}}'
# 把打印出的 image ID 贴进你自己那份 suite 的 environment.image。
#
# 下面两样都从 npm 官方 registry 拉，构建时需要联网：
#   openclaw                  https://www.npmjs.com/package/openclaw
#   @openclaw/qwen-provider   https://www.npmjs.com/package/@openclaw/qwen-provider
#   @openclaw/deepseek-provider  https://www.npmjs.com/package/@openclaw/deepseek-provider
#   基础镜像 node:24-slim     https://hub.docker.com/_/node
# 源码（只读参考，不需要 clone）：https://github.com/openclaw/openclaw
FROM node:24-slim

# openclaw 会 spawn shell 工具（read/write/exec），slim 里缺 git 会静默少一半能力
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g openclaw@2026.7.1-2

# profile 与宿主机同名，adapter 的 --profile skilleval 不用分叉。
# 装两家 provider：装哪家不决定用哪家 —— 用哪家由 suite 的
# runtime_options.auth_choice + model 决定（两者都进 fingerprint）。
RUN openclaw --profile skilleval plugins install @openclaw/qwen-provider \
    && openclaw --profile skilleval plugins install @openclaw/deepseek-provider

# 这里**故意不 onboard**。实测：onboard 会把 key 写进 profile 的 auth store，而
# auth store 优先于环境变量 —— 拿占位 key build 出来的镜像，运行时再传真
# QWEN_API_KEY 也照样 401（OPENCLAW.md §4 那句「运行时认三个 env 变量」只在
# auth store 里没有记录时成立）。所以 onboard 必须带着真 key 在运行时做，
# 由 openclaw adapter 在容器起来后执行（约 6s/容器）。
# 好处是真 key 一层都不进镜像：镜像可以随便存、随便分发。

WORKDIR /workspace
