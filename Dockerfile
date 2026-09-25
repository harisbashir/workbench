# Workbench production image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN useradd --create-home --uid 1000 workbench

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# collectstatic needs a key at build time only; the real key comes from the environment at runtime.
RUN WORKBENCH_SECRET_KEY=build-only python manage.py collectstatic --noinput \
    && mkdir -p /app/media && chown -R workbench:workbench /app/media

USER workbench
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60 --access-logfile -"]
