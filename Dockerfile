FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY tools ./tools
COPY fixtures ./fixtures
COPY licenses ./licenses
COPY README.md ATTRIBUTION.md ./
RUN pip install --no-deps . && useradd --uid 10001 --create-home techdocs \
    && mkdir -p /data /app/models && chown -R techdocs:techdocs /data /app/models
ENV TECHDOCS_DB_PATH=/data/techdocs.sqlite3 \
    TECHDOCS_DATA_ROOT=/data \
    TECHDOCS_ORIGIN=http://127.0.0.1:8377 \
    TECHDOCS_MODEL_DIR=/app/models \
    PYTHONUNBUFFERED=1
USER techdocs
EXPOSE 8377
HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-m", "tools.container_health"]
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8377", "--workers", "1"]
