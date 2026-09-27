# syntax=docker/dockerfile:1.7
# ASAS service image: hash-pinned dependencies, non-root, read-only friendly, health-checked.
#   docker build -t asas .
#   docker run --rm -p 8000:8000 -e ASAS_DATABASE_URL=postgresql://... asas
FROM python:3.11-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY requirements/prod.lock requirements/prod.lock
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes -r requirements/prod.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN /opt/venv/bin/pip install --no-deps .

FROM python:3.11-slim
LABEL org.opencontainers.image.title="asas" \
      org.opencontainers.image.description="Auditable agentic surveillance"
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin asas \
 && mkdir -p /var/lib/asas/anchors && chown asas:asas /var/lib/asas/anchors
COPY --from=build /opt/venv /opt/venv
COPY config /app/config
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ASAS_CONFIG_DIR=/app/config
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=4).status == 200 else 1)"
ENTRYPOINT ["asas"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
