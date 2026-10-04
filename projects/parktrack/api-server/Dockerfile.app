FROM python:3.10-slim

WORKDIR /app

# Install system dependencies for OpenCV
# Removed gcc and libpq-dev as we use psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Environment variables will be passed via docker-compose
# CMD ["python", "src/main.py"]
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
