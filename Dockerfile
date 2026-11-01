FROM python:3.11-slim

# Install system dependencies including ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create downloads folder
RUN mkdir -p /app/downloads

ENV PORT=5000
EXPOSE 5000

CMD ["sh", "-c", "uvicorn web_app:app --host 0.0.0.0 --port ${PORT:-5000}"]
