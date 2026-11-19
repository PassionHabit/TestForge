FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY testforge ./testforge
COPY alembic.ini ./
COPY migrations ./migrations
RUN pip install --no-cache-dir '.[postgres]'

RUN useradd --create-home --uid 10001 testforge
USER testforge
EXPOSE 8000
CMD ["testforge-api"]
