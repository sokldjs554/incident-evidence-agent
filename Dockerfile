FROM python:3.12-slim-bookworm AS app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --chown=10001:10001 pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt \
    python -m pip install --cert /etc/ssl/certs/ca-certificates.crt '.[incident]'

ARG FIVEWHYS_REVISION=local
ENV FIVEWHYS_REVISION=$FIVEWHYS_REVISION
LABEL org.opencontainers.image.revision=$FIVEWHYS_REVISION

USER 10001:10001
CMD ["uvicorn", "fivewhys.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM app AS testing

USER root
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=secret,id=build_ca,target=/etc/ssl/certs/ca-certificates.crt \
    python -m pip install --cert /etc/ssl/certs/ca-certificates.crt '.[dev,mcp,es]'
COPY --chown=10001:10001 tests ./tests
USER 10001:10001
ENTRYPOINT ["python", "-m", "pytest", "-o", "cache_dir=/tmp/pytest_cache"]
CMD []
