# Use Python 3.11 slim image for smaller size
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
# Split requirements into base and optional for optimization
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir numpy Pillow

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p naditwin/rivers

# Expose port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/rivers')" || exit 1

# Run the application
CMD ["python", "run.py", "--port", "8080", "--no-browser"]
