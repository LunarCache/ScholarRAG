FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra eval
COPY docs ./docs
COPY evals ./evals
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "scholarrag.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
