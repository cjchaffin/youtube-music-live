FROM python:3.11-slim

# Install system dependencies including FFmpeg and build-essential
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and dashboard assets
COPY src/ ./src/
COPY web/ ./web/

# Expose FastAPI dashboard port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]
