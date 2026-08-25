FROM python:3.12-slim

ENV UWAZI_REPOSITORY_PATH=/home/app/uwazi \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv \
    && curl -fsSL https://opencode.ai/install | bash \
    && mv /root/.opencode/bin/opencode /usr/local/bin/opencode \
    && apt-get purge -y curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app \
    && git clone --depth 1 https://github.com/huridocs/uwazi.git "$UWAZI_REPOSITORY_PATH" \
    && chown -R app:app "$UWAZI_REPOSITORY_PATH"

COPY --chown=app:app pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app . .
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app
