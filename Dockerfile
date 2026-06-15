FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 app \
    && chown -R app:app /app

COPY --chown=app:app requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

USER app
