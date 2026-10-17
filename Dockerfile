FROM python:3.11-slim

# Install system dependencies: ffmpeg and certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy server script
COPY server.py .

# Configure default environment for 24/7 cloud hosting
ENV YOUTUBE_HOST=0.0.0.0
ENV PORT=7860
ENV YOUTUBE_ALLOWED_ORIGINS=*

EXPOSE 7860

CMD ["python", "server.py"]
