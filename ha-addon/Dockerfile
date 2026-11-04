ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_VERSION=0.0.0
ARG INTERVALS_MCP_REF=main
ARG INTERVALS_MCP_REPO=https://github.com/bjorngadde/intervals-icu-mcp

# Home Assistant builds add-ons from this folder only, so the server is fetched
# from git at INTERVALS_MCP_REF. Dependencies are installed from the committed
# uv.lock with hashes (pip hash-checking mode), then the package itself without
# deps. Every locked dependency ships musllinux wheels for amd64 and aarch64.
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${BUILD_VERSION}
RUN set -eux; \
    apk add --no-cache --virtual .build-deps git; \
    git clone --filter=blob:none "${INTERVALS_MCP_REPO}" /tmp/src; \
    git -C /tmp/src checkout --detach "${INTERVALS_MCP_REF}"; \
    pip install --no-cache-dir uv==0.11.32; \
    uv export --project /tmp/src --frozen --no-dev --no-emit-project -o /tmp/requirements.txt; \
    pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt; \
    pip install --no-cache-dir --no-deps /tmp/src; \
    pip uninstall -y uv; \
    rm -rf /tmp/src /tmp/requirements.txt; \
    apk del .build-deps

COPY run.py /run.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -m intervals_icu_mcp.healthcheck --port 8099

CMD ["python3", "/run.py"]
