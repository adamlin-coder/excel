# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CSV_DATA_DIR=/data CSV_CONFIG_FILE=/run/secrets/csv_config
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
RUN groupadd --gid 10001 csvapp && useradd --uid 10001 --gid 10001 --no-create-home csvapp \
    && mkdir /data && chown csvapp:csvapp /data
COPY --chown=csvapp:csvapp app ./app
USER csvapp
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
