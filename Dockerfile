# Production Dockerfile for Telegram Bot with FFmpeg and yt-dlp
FROM python:3.11-slim-bookworm

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Install system dependencies: ffmpeg, ffprobe, git, curl, ca-certificates
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Verify ffmpeg installation
RUN ffmpeg -version && ffprobe -version

# Set working directory
WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code and default assets
COPY bot/ ./bot/
COPY cover.jpg ./
COPY .env.example ./

# Create persistent storage directories
RUN mkdir -p storage/downloads storage/processed storage/temp logs

# Command to run the bot
CMD ["python", "-m", "bot.main"]
