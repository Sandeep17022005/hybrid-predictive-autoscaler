import time
import random
import logging
import signal
import sys
from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Hybrid Predictive AutoScaler - Application Service")

# Prometheus Metrics
REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP Requests",
    ["method", "endpoint", "status_code"]
)

LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP Request Duration in seconds",
    ["method", "endpoint"]
)

@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    """Monitor HTTP requests and record metrics with error handling."""
    start_time = time.time()
    
    try:
        # Process request
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        logger.error(f"Exception in request handler: {e}")
        # Record error metrics and re-raise
        duration_seconds = time.time() - start_time
        REQUESTS.labels(
            method=request.method,
            endpoint=request.url.path,
            status_code=500
        ).inc()
        LATENCY.labels(
            method=request.method,
            endpoint=request.url.path
        ).observe(duration_seconds)
        raise
    
    # Record metrics
    duration_seconds = time.time() - start_time
    REQUESTS.labels(
        method=request.method,
        endpoint=request.url.path,
        status_code=status_code
    ).inc()
    
    LATENCY.labels(
        method=request.method,
        endpoint=request.url.path
    ).observe(duration_seconds)
    
    return response

@app.get("/")
def read_root():
    """Root endpoint with simulated processing."""
    # Simulate some processing time
    time.sleep(random.uniform(0.01, 0.05))
    return {"message": "Hello from the Application Service!"}

@app.get("/health")
def health_check():
    """Health check endpoint for Kubernetes probes."""
    return {"status": "healthy"}

@app.get("/heavy")
def heavy_processing():
    """Heavy processing endpoint - CPU bound operation with timeout."""
    try:
        # Simulate heavy processing (CPU bound)
        # Limit computation to prevent OOM
        max_computation = min(1000000, random.randint(500000, 1000000))
        sum_n = sum(i * i for i in range(max_computation))
        return {"message": "Heavy processing complete", "result": sum_n}
    except Exception as e:
        logger.error(f"Error in heavy_processing: {e}")
        return {"message": "Processing error", "error": str(e)}, 500

@app.get("/metrics")
def metrics():
    """Expose Prometheus metrics."""
    # Expose Prometheus metrics
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

def signal_handler(sig, frame):
    """Handle graceful shutdown on SIGTERM."""
    logger.info(f"Received signal {sig}, shutting down gracefully...")
    sys.exit(0)

if __name__ == "__main__":
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    import uvicorn
    logger.info("Starting application service...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
