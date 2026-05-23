# ── Swiggy Offer Telegram Bot ──────────────────────────────────────────────────
# Base: official Playwright + Python image (Chromium pre-installed)
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-install Chromium browser binary
RUN playwright install chromium --with-deps

# Copy application source
COPY bot.py database.py ./

# Create persistent data directory (mounted as volume on Railway)
RUN mkdir -p /data

# Expose nothing (bot uses polling)
CMD ["python", "bot.py"]
