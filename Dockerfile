# Railway picks this up by itself. Keep COPY in sync
# with what runs in production: the package, migrations and alembic.ini.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv/app

COPY pyproject.toml README.md ./
COPY finance ./finance
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install .

EXPOSE 8000
# Migrations and reference data are idempotent; running them here keeps every
# start consistent whatever the platform's pre-deploy settings are.
CMD ["sh", "-c", "alembic upgrade head && finance seed && exec uvicorn finance.web.app:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
