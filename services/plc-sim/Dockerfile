# Multi-stage: dependencies resolve once, the runtime image carries no build tools.
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependencies first: this layer is cached until pyproject/uv.lock change,
# so editing source does not reinstall pymodbus every build.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# Never run as root: a simulator has no reason to hold privileges.
RUN useradd --create-home --uid 10001 plcsim

WORKDIR /app
COPY --from=builder --chown=plcsim:plcsim /app/.venv /app/.venv
COPY --from=builder --chown=plcsim:plcsim /app/src /app/src
COPY --chown=plcsim:plcsim config /app/config

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    TAGS_FILE=/app/config/tags.yaml \
    BIND_HOST=0.0.0.0 \
    BIND_PORT=5020

USER plcsim
EXPOSE 5020

# Proves the TCP listener is up, which is what a client actually depends on.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import socket,os; s=socket.create_connection(('127.0.0.1', int(os.environ['BIND_PORT'])), 2); s.close()"

CMD ["python", "-m", "plc_sim.main"]
