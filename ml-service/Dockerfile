FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN useradd -m -u 1000 appuser

# Install build dependencies for statsmodels/scipy if needed
# Note: Pre-built wheels usually suffice on python:3.10-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ build-essential curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and model
COPY main.py model.py ./

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Run application with unbuffered output for better logging
CMD ["python", "-u", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
