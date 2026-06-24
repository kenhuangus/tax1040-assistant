FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080
WORKDIR /app

# deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code + the official IRS form (assets/irs/f1040--2025.pdf must ship in the image)
COPY . .

# non-root
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8080
# shell form so ${PORT} expands at runtime; Cloud Run injects PORT=8080
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
