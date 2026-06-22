FROM python:3.12-slim

ENV UWAZI_REPOSITORY_PATH=/home/app/uwazi

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
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

COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

USER app
