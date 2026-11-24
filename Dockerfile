FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUDIT_DB=/data/audit.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 2 worker × 4 线程；写入走 BEGIN IMMEDIATE + busy_timeout 重试，SQLite WAL 支持并发补报
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--threads=4", "--timeout=60", "app.__main__:app"]
