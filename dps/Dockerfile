# Document Processing Service. One image, three roles chosen by the command:
#   API:          uvicorn --factory dps.main:create_app --host 0.0.0.0 --port 9400
#   worker:       python -m dps.worker                       (DPS_WORKER_POOL=STANDARD)
#   heavy worker: python -m dps.worker  with INSTALL_HEAVY=1  (DPS_WORKER_POOL=HEAVY, Docling, CPU torch)
FROM python:3.12-slim
ARG INSTALL_HEAVY=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CREWAI_TRACING_ENABLED=false \
    CREWAI_DISABLE_TELEMETRY=true CREWAI_DISABLE_TRACKING=true
# apt-get upgrade takes the distribution's security fixes released after the base image was built. A failed download
# fails the build (--error-on=any) rather than shipping an unpatched image (CI, 11 November 2026).
RUN useradd --system --create-home --uid 10001 dps && apt-get -o Acquire::Retries=5 update --error-on=any \
    && apt-get -o Acquire::Retries=5 upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir ".[worker,mssql,mysql]" \
    && if [ "$INSTALL_HEAVY" = "1" ]; then          pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu ".[heavy]"; fi
# Every image carries the PP-OCRv6 text model for the fast OCR: RapidOCR's own model is for Chinese and runs English
# words together (bulk run, 22 Nov 2026). Its SHA-256 is checked: a changed file fails the build. A bank network that
# blocks modelscope.cn serves the same file from its own artifact store (DPS_OCR_MODEL_URL at build time).
ARG DPS_OCR_MODEL_URL=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.0/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx
ENV DPS_OCR_REC_MODEL=/opt/ocr-models/PP-OCRv6_rec_small.onnx
RUN python -c "import hashlib, os, sys, urllib.request; \
data = urllib.request.urlopen(sys.argv[1], timeout=600).read(); \
digest = hashlib.sha256(data).hexdigest(); \
assert digest == '6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884', digest; \
os.makedirs('/opt/ocr-models', exist_ok=True); open(os.environ['DPS_OCR_REC_MODEL'], 'wb').write(data)" \
    "$DPS_OCR_MODEL_URL"
# The heavy image carries Docling's models (layout, tables, RapidOCR): the parse sandbox has no network, so they
# cannot be downloaded at first use (bulk run, 22 Nov 2026: every Docling run failed without them).
ENV DPS_DOCLING_ARTIFACTS=/opt/docling-models HF_HUB_OFFLINE=1
RUN if [ "$INSTALL_HEAVY" = "1" ]; then \
        HF_HUB_OFFLINE=0 docling-tools models download layout tableformer rapidocr -o /opt/docling-models; fi
RUN mkdir -p /data/storage /data/staging /data/derived /run/dps-parser /home/dps/.cache \
    && chown -R dps /data /run/dps-parser /home/dps/.cache
USER 10001
ENV DPS_DATABASE_URL=sqlite:////data/dps.db DPS_STORAGE_DIR=/data/storage DPS_STAGING_DIR=/data/staging \
    DPS_DERIVED_DIR=/data/derived
EXPOSE 9400
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory dps.main:create_app --host 0.0.0.0 --port 9400"]
