# Shadowfenced one-command run:
#   docker build -t shadowfenced . && docker run --env-file .env -p 8765:8765 shadowfenced
# Or with compose: docker compose up --build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 sfuser

WORKDIR /app

COPY pyproject.toml README.md LICENSE policy.yaml ./
COPY src ./src

RUN pip install --no-cache-dir . \
    && mkdir -p /app/runs \
    && chown -R sfuser:sfuser /app

USER sfuser

EXPOSE 8765

CMD ["uvicorn", "shadowfenced.app:app", "--host", "0.0.0.0", "--port", "8765"]
