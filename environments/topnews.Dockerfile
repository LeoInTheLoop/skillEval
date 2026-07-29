# topnews 的隔离执行镜像。skill 正文不会 bake 进镜像：Environment Backend 会将
# skills_full/topnews 的精确快照按 request 复制并只读挂载到 /skills/topnews。
# 因而改 skill V2 不必重建运行时镜像；镜像只固定 Python/系统依赖，skill 内容仍进
# config_hash 和 inputs 快照。
#
# Build:
#   docker build -f environments/topnews.Dockerfile -t skilleval-topnews .
#   docker image inspect skilleval-topnews --format '{{.Id}}'
# Build this only from the local base produced by environments/openclaw.Dockerfile.
# Dockerfile FROM cannot address a local image by its bare sha256 ID; reproducibility
# is enforced at consumption time by the final image ID in the suite.
FROM skilleval-openclaw

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Requirements are pinned by the downloaded skill snapshot.  They are
# installed in the runtime image, never into the host Python environment.
COPY skills_full/topnews/requirements.txt /tmp/topnews-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/topnews-requirements.txt
