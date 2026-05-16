FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir ".[test]"

COPY src/ src/
COPY fixtures/ fixtures/
COPY tests/ tests/

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
