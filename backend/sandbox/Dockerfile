# ============================================================
# Sandbox image — isolated execution environment
# All commands run here, never on the host
# ============================================================
# Keep the runtime and npm patched independently: Node's bundled npm can lag behind.
FROM node:24.22.2-bookworm-slim AS node-runtime

FROM python:3.13-slim

ARG INSTALL_OFFICE_RENDERER=false

RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    git curl wget jq tree ripgrep vim-common ca-certificates libstdc++6 libatomic1 file poppler-utils poppler-data \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && if [ "$INSTALL_OFFICE_RENDERER" = "true" ]; then \
         apt-get install -y --no-install-recommends \
           libreoffice-writer libreoffice-calc libreoffice-impress fonts-noto-cjk; \
       fi \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && npm install --global --ignore-scripts npm@11.21.2 \
    && rm -rf /root/.npm

RUN useradd -m -u 1000 -s /bin/bash sandbox
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace && chmod 700 /workspace

RUN pip install --no-cache-dir --break-system-packages \
    requests httpx beautifulsoup4 pandas numpy duckdb \
    pdfplumber==0.11.9 pypdfium2==5.8.0 python-docx==1.2.0 \
    openpyxl==3.1.5 python-pptx==1.0.2 Pillow==13.0.0

WORKDIR /workspace
USER sandbox
# Validate the copied runtime and npm entrypoints as the actual sandbox user
# on every target architecture, including CI's amd64 and arm64 builds.
RUN test "$(node --version)" = "v24.22.2" \
    && test "$(npm --version)" = "11.21.2" && npx --version
CMD ["sleep", "infinity"]
