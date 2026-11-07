# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ALGO_DATA_DIR=/data

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY examples ./examples
COPY bench ./bench

ENV PYTHONPATH=/app/src

RUN useradd --create-home --uid 10001 algo && mkdir -p /data && chown -R algo /data
USER algo

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"

CMD ["uvicorn", "algo_platform.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
