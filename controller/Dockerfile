FROM python:3.10-slim

# Set working directory
WORKDIR /app

# Create non-root user for security
RUN useradd -m -u 1000 appuser

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY controller.py .

# Change ownership to non-root user
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Run controller with unbuffered output for better logging
# Use exec form to ensure signals are properly handled for graceful shutdown
CMD ["python", "-u", "controller.py"]
