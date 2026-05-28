FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN useradd -m -u 1000 appuser

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy load testing script
COPY locustfile.py .

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Run locust load test with environment-based host configuration
CMD ["sh", "-c", "python -u -m locust -f locustfile.py --host=${LOCUST_HOST:-http://app-service:8000} --headless -u 10 -r 2"]
