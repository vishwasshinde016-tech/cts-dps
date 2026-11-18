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
RUN pip install --no-cache-dir ".[worker,mssql]" \
    && if [ "$INSTALL_HEAVY" = "1" ]; then          pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu ".[heavy]"; fi
# /home/dps/.cache holds the Docling models the heavy worker downloads on first use.
RUN mkdir -p /data/storage /data/staging /data/derived /run/dps-parser /home/dps/.cache \
    && chown -R dps /data /run/dps-parser /home/dps/.cache
USER 10001
ENV DPS_DATABASE_URL=sqlite:////data/dps.db DPS_STORAGE_DIR=/data/storage DPS_STAGING_DIR=/data/staging \
    DPS_DERIVED_DIR=/data/derived
EXPOSE 9400
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory dps.main:create_app --host 0.0.0.0 --port 9400"]
