# Workbench — one container, one data folder.
#   docker compose up -d        (see README / install.sh)
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WORKBENCH_DATA_DIR=/data

WORKDIR /app
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin workbench

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN rm -rf data && WORKBENCH_DATA_DIR=/tmp/build-data python manage.py collectstatic --noinput -v0 \
    && rm -rf /tmp/build-data \
    && chmod -R a+rX,go-w /app && chmod +x docker-entrypoint.sh \
    && install -m 755 deploy/manage.sh /usr/local/bin/manage

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["serve"]
