FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 trading \
    && useradd --uid 10001 --gid trading --no-create-home trading \
    && mkdir /app/staticfiles && chown trading:trading /app/staticfiles
COPY manage.py ./
COPY config ./config
COPY trading ./trading
USER trading
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-"]
