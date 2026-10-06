FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml poetry.lock ./
RUN pip install poetry==1.8.3 && poetry config virtualenvs.create false \
    && poetry install --only main -E ocr --no-root --no-interaction
COPY backend ./backend
CMD ["uvicorn", "backend.api:app", "--host", "0.0.0.0", "--port", "8000"]
