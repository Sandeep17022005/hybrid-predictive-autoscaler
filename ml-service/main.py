import os
import time
import requests
import asyncio
import logging
from fastapi import FastAPI
from pydantic import BaseModel
from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response
from model import ResourcePredictor

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="ML Prediction Service")

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-service.default.svc.cluster.local:9090")
PREDICTION_HORIZON_MINUTES = int(os.getenv("PREDICTION_HORIZON_MINUTES", "5"))
PROMETHEUS_RETRIES = int(os.getenv("PROMETHEUS_RETRIES", "3"))

# Observability metrics
PREDICTION_REQUESTS = Counter("ml_prediction_requests_total", "Total prediction API requests")
ML_PREDICTED_RPS = Gauge("ml_predicted_rps", "Predicted requests per second")
ML_PREDICTION_LOWER = Gauge("ml_prediction_lower_bound", "Predicted lower confidence bound")
ML_PREDICTION_UPPER = Gauge("ml_prediction_upper_bound", "Predicted upper confidence bound")
ML_MODEL_TRAININGS = Counter("ml_model_trainings_total", "Total model training attempts")
ML_MODEL_TRAINING_SUCCESS = Counter("ml_model_training_success_total", "Successful model training runs")

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.get("/health")
def health_check():
    """Health check endpoint for Kubernetes probes."""
    return {"status": "healthy", "model_trained": last_prediction > 0}

predictor = ResourcePredictor(order=(2, 1, 2))
# Use asyncio.Lock to prevent race conditions on global state
_state_lock = asyncio.Lock()
last_prediction = 0.0
last_confidence_lower = 0.0
last_confidence_upper = 0.0

class PredictionResponse(BaseModel):
    predicted_rps: float
    horizon_mins: int
    status: str
    confidence_lower: float | None = None
    confidence_upper: float | None = None

def fetch_prometheus_data() -> list[float]:
    """
    Fetch the last 30 minutes of request rate (RPS) data from Prometheus.
    Includes retry logic with exponential backoff for resilience.
    """
    query = 'sum(rate(http_requests_total[1m]))'
    end_time = time.time()
    start_time = end_time - (30 * 60) # 30 mins ago
    step = '60s' # 1 data point per minute
    
    for attempt in range(PROMETHEUS_RETRIES):
        try:
            response = requests.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": start_time,
                    "end": end_time,
                    "step": step
                },
                timeout=5
            )
            response.raise_for_status()
            data = response.json()
            
            if data['status'] == 'success' and data['data']['result']:
                values = data['data']['result'][0]['values']
                # values is a list of [timestamp, string_value]
                logger.info(f"Successfully fetched {len(values)} data points from Prometheus")
                return [float(v[1]) for v in values]
            else:
                logger.warning(f"Prometheus returned no data. Status: {data.get('status')}")
                return []
        except requests.exceptions.Timeout:
            logger.warning(f"Prometheus timeout (attempt {attempt + 1}/{PROMETHEUS_RETRIES})")
            if attempt < PROMETHEUS_RETRIES - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"Failed to connect to Prometheus at {PROMETHEUS_URL} (attempt {attempt + 1}/{PROMETHEUS_RETRIES}): {e}")
            if attempt < PROMETHEUS_RETRIES - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
        except Exception as e:
            logger.error(f"Error fetching Prometheus data: {e}")
            if attempt < PROMETHEUS_RETRIES - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    
    logger.error(f"Failed to fetch Prometheus data after {PROMETHEUS_RETRIES} attempts")
    return []

async def train_model_loop():
    """
    Periodically train the ARIMA model on historical Prometheus data.
    Uses asyncio.Lock to safely update global prediction state.
    """
    global last_prediction, last_confidence_lower, last_confidence_upper
    
    while True:
        try:
            logger.info("Fetching data and training model...")
            data = fetch_prometheus_data()
            ML_MODEL_TRAININGS.inc()

            if data:
                success = predictor.train(data)
                if success:
                    ML_MODEL_TRAINING_SUCCESS.inc()
                    result = predictor.predict_next(steps=PREDICTION_HORIZON_MINUTES)
                    predictions = result.get("predictions", [])
                    conf_int = result.get("conf_int", [])

                    if predictions:
                        predicted_max = max(predictions)
                        
                        # Use lock to safely update global state
                        async with _state_lock:
                            last_prediction = predicted_max
                            if conf_int:
                                last_confidence_lower = conf_int[-1]["lower"]
                                last_confidence_upper = conf_int[-1]["upper"]
                            
                            logger.info(f"Model trained successfully. Next predicted max RPS: {predicted_max:.2f}")
                            # Set observability metrics for final point
                            ML_PREDICTED_RPS.set(predicted_max)
                            if conf_int:
                                ML_PREDICTION_LOWER.set(conf_int[-1]["lower"])
                                ML_PREDICTION_UPPER.set(conf_int[-1]["upper"])
                else:
                    logger.warning("Not enough data to train model.")
            else:
                logger.warning("No data from Prometheus.")
        except Exception as e:
            logger.error(f"Error in training loop: {e}")

        # Re-train every 1 minute
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(train_model_loop())

@app.get("/predict", response_model=PredictionResponse)
async def get_prediction():
    """
    Return the latest model prediction with confidence bounds.
    Uses asyncio.Lock to safely read global prediction state.
    """
    PREDICTION_REQUESTS.inc()
    
    # Use lock to safely read global state
    async with _state_lock:
        pred = last_prediction
        conf_lower = last_confidence_lower
        conf_upper = last_confidence_upper

    status = "ready" if pred > 0 else "insufficient_data"
    safe_pred = pred if pred > 0 else 5.0

    return PredictionResponse(
        predicted_rps=safe_pred,
        horizon_mins=PREDICTION_HORIZON_MINUTES,
        status=status,
        confidence_lower=conf_lower if pred > 0 else None,
        confidence_upper=conf_upper if pred > 0 else None
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
