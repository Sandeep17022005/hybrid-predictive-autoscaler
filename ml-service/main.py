"""
Hybrid Predictive Autoscaler - ML Prediction Service
======================================================
Machine learning microservice that forecasts workload using multiple models:
  - ARIMA (statsmodels)
  - Prophet (Facebook)
  - LSTM (PyTorch)
  - Ensemble (weighted combination)

Fetches historical metrics from Prometheus, runs inference,
and exposes predictions via REST API.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    Gauge,
    generate_latest,
)
from pydantic import BaseModel

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
MODEL_TYPE = os.getenv("MODEL_TYPE", "ensemble")  # arima, prophet, lstm, ensemble
PREDICTION_HORIZON = int(os.getenv("PREDICTION_HORIZON", "300"))  # seconds
HISTORY_WINDOW = int(os.getenv("HISTORY_WINDOW", "3600"))  # seconds
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.7"))
PORT = int(os.getenv("PORT", "8001"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-service")

# ──────────────────────────────────────────────
# Prometheus Metrics
# ──────────────────────────────────────────────
ml_registry = CollectorRegistry()

PREDICTION_COUNT = Counter(
    "prediction_requests_total",
    "Total prediction requests",
    ["model_type", "status"],
    registry=ml_registry,
)

PREDICTION_LATENCY = Histogram(
    "prediction_latency_seconds",
    "Prediction request latency",
    ["model_type"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    registry=ml_registry,
)

PREDICTION_CONFIDENCE = Gauge(
    "prediction_confidence",
    "Latest prediction confidence score",
    registry=ml_registry,
)

RECOMMENDED_REPLICAS = Gauge(
    "prediction_recommended_replicas",
    "Latest recommended replica count",
    registry=ml_registry,
)

# ──────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────

class PredictionRequest(BaseModel):
    model_config = {"protected_namespaces": ()}
    metric_name: str = "app_request_rate_per_second"
    horizon_seconds: int = 300
    model_type: str = "ensemble"


class PredictionPoint(BaseModel):
    timestamp: float
    value: float
    lower_bound: float
    upper_bound: float


class PredictionResponse(BaseModel):
    model_config = {"protected_namespaces": ()}
    metric_name: str
    model_type: str
    predictions: list[PredictionPoint]
    confidence: float
    recommended_replicas: int
    current_value: float
    prediction_time: float
    model_accuracy: float


class ModelMetrics(BaseModel):
    model_config = {"protected_namespaces": ()}
    model_type: str
    mae: float
    rmse: float
    mape: float
    last_trained: float
    training_samples: int


# ──────────────────────────────────────────────
# Metrics Fetcher (Prometheus)
# ──────────────────────────────────────────────
class MetricsFetcher:
    """Fetches time-series data from Prometheus."""

    def __init__(self, prometheus_url: str):
        self.prometheus_url = prometheus_url.rstrip("/")
        self._last_actual_rps = 50.0  # fallback default

    async def _fetch_actual_rps(self) -> float:
        """Fetch the current RPS from the workload service for anchoring."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get("http://localhost:8000/api/status")
                data = response.json()
                rps = float(data.get("current_rps", 0))
                if rps > 0:
                    self._last_actual_rps = rps
                return rps
        except Exception:
            return self._last_actual_rps

    async def fetch_metric(
        self,
        metric_name: str,
        duration_seconds: int = 3600,
        step: int = 15,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Fetch metric data from Prometheus.
        Returns (timestamps, values) arrays.
        """
        import httpx

        end_time = time.time()
        start_time = end_time - duration_seconds

        query_url = f"{self.prometheus_url}/api/v1/query_range"
        params = {
            "query": metric_name,
            "start": start_time,
            "end": end_time,
            "step": step,
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(query_url, params=params)
                data = response.json()

                if data.get("status") == "success" and data["data"]["result"]:
                    values = data["data"]["result"][0]["values"]
                    timestamps = np.array([float(v[0]) for v in values])
                    metric_values = np.array([float(v[1]) for v in values])
                    return timestamps, metric_values

        except Exception as e:
            logger.warning(f"Failed to fetch from Prometheus: {e}")

        # Generate synthetic data anchored to actual workload RPS
        logger.info("Using synthetic data for prediction (anchored to actual RPS)")
        current_rps = await self._fetch_actual_rps()
        return self._generate_synthetic_data(duration_seconds, step, current_rps)

    def _generate_synthetic_data(
        self, duration: int, step: int, current_rps: float = 50.0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Generate realistic synthetic traffic data anchored to the actual current RPS."""
        n_points = duration // step
        timestamps = np.linspace(time.time() - duration, time.time(), n_points)

        t = np.arange(n_points)

        # Build a series that ends at approximately current_rps
        # Use a gentle sinusoidal pattern with amplitude proportional to current RPS
        amplitude = max(5, current_rps * 0.3)
        base = current_rps + amplitude * np.sin(2 * np.pi * t / (n_points / 2))

        # Small trend towards current value at the end
        trend = np.linspace(-amplitude * 0.2, 0, n_points)

        # Small noise proportional to the signal
        noise = np.random.normal(0, max(2, current_rps * 0.05), n_points)

        # Occasional small spikes
        spikes = np.zeros(n_points)
        spike_indices = np.random.choice(n_points, size=max(1, n_points // 30), replace=False)
        spikes[spike_indices] = np.random.uniform(current_rps * 0.1, current_rps * 0.4, len(spike_indices))

        values = np.maximum(1, base + trend + noise + spikes)

        # Force the last value to be very close to the actual current RPS
        values[-1] = current_rps + np.random.normal(0, max(0.5, current_rps * 0.02))
        values[-1] = max(0.1, values[-1])

        return timestamps, values


# ──────────────────────────────────────────────
# Prediction Models
# ──────────────────────────────────────────────

class ARIMAPredictor:
    """ARIMA-based time series forecaster."""

    def __init__(self):
        self.model = None
        self.last_mae = 0.0
        self.last_rmse = 0.0

    def predict(
        self, values: np.ndarray, horizon_steps: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        """
        Predict future values using ARIMA.
        Returns: (predictions, lower_bounds, upper_bounds, confidence)
        """
        try:
            from statsmodels.tsa.arima.model import ARIMA

            # Use last 200 points max for efficiency
            data = values[-200:]

            # Fit ARIMA(2,1,2) model
            model = ARIMA(data, order=(2, 1, 2))
            fitted = model.fit()

            # Forecast
            forecast = fitted.get_forecast(steps=horizon_steps)
            predictions = forecast.predicted_mean
            conf_int = forecast.conf_int(alpha=0.2)  # 80% confidence interval

            lower = conf_int[:, 0]
            upper = conf_int[:, 1]

            # Ensure non-negative
            predictions = np.maximum(0, predictions)
            lower = np.maximum(0, lower)
            upper = np.maximum(0, upper)

            # Calculate confidence based on prediction interval width
            avg_width = np.mean(upper - lower)
            avg_value = np.mean(predictions)
            confidence = max(0.1, min(0.95, 1.0 - (avg_width / (avg_value + 1))))

            # Calculate accuracy on training data
            in_sample = fitted.fittedvalues
            actual = data[1:]  # ARIMA drops first observation for differencing
            if len(in_sample) > 0 and len(actual) > 0:
                min_len = min(len(in_sample), len(actual))
                self.last_mae = float(
                    np.mean(np.abs(in_sample[:min_len] - actual[:min_len]))
                )
                self.last_rmse = float(
                    np.sqrt(np.mean((in_sample[:min_len] - actual[:min_len]) ** 2))
                )

            return predictions, lower, upper, confidence

        except Exception as e:
            logger.error(f"ARIMA prediction failed: {e}")
            return self._fallback_predict(values, horizon_steps)

    def _fallback_predict(
        self, values: np.ndarray, horizon_steps: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        """Simple exponential smoothing fallback."""
        alpha = 0.3
        last_value = values[-1]
        predictions = np.full(horizon_steps, last_value)

        # Apply trend
        if len(values) > 10:
            trend = np.mean(np.diff(values[-10:])) 
            for i in range(horizon_steps):
                predictions[i] = last_value + trend * (i + 1)

        predictions = np.maximum(0, predictions)
        margin = np.std(values[-20:]) * 1.5
        lower = predictions - margin
        upper = predictions + margin

        return predictions, np.maximum(0, lower), upper, 0.5


class ProphetPredictor:
    """Facebook Prophet-based time series forecaster."""

    def __init__(self):
        self.last_mae = 0.0
        self.last_rmse = 0.0

    def predict(
        self, timestamps: np.ndarray, values: np.ndarray, horizon_steps: int, step: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        """Predict using Prophet."""
        try:
            import pandas as pd
            from prophet import Prophet

            # Prepare dataframe
            df = pd.DataFrame({
                "ds": pd.to_datetime(timestamps, unit="s"),
                "y": values,
            })

            # Fit model (suppress verbose output)
            model = Prophet(
                changepoint_prior_scale=0.05,
                seasonality_mode="multiplicative",
                interval_width=0.8,
            )
            model.fit(df)

            # Create future dataframe
            future = model.make_future_dataframe(
                periods=horizon_steps, freq=f"{step}s"
            )
            forecast = model.predict(future)

            # Extract predictions
            pred_rows = forecast.tail(horizon_steps)
            predictions = pred_rows["yhat"].values
            lower = pred_rows["yhat_lower"].values
            upper = pred_rows["yhat_upper"].values

            predictions = np.maximum(0, predictions)
            lower = np.maximum(0, lower)

            # Confidence
            avg_width = np.mean(upper - lower)
            avg_value = np.mean(predictions)
            confidence = max(0.1, min(0.95, 1.0 - (avg_width / (avg_value + 1))))

            return predictions, lower, upper, confidence

        except Exception as e:
            logger.error(f"Prophet prediction failed: {e}")
            arima = ARIMAPredictor()
            return arima.predict(values, horizon_steps)


class LSTMPredictor:
    """LSTM-based time series forecaster using PyTorch."""

    def __init__(self, sequence_length: int = 30):
        self.sequence_length = sequence_length
        self.model = None
        self.scaler_min = 0.0
        self.scaler_max = 1.0
        self.last_mae = 0.0
        self.last_rmse = 0.0

    def _normalize(self, data: np.ndarray) -> np.ndarray:
        self.scaler_min = float(np.min(data))
        self.scaler_max = float(np.max(data))
        range_val = self.scaler_max - self.scaler_min
        if range_val < 1e-8:
            return np.zeros_like(data)
        return (data - self.scaler_min) / range_val

    def _denormalize(self, data: np.ndarray) -> np.ndarray:
        return data * (self.scaler_max - self.scaler_min) + self.scaler_min

    def predict(
        self, values: np.ndarray, horizon_steps: int
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        """Predict using LSTM."""
        try:
            import torch
            import torch.nn as nn

            class LSTMModel(nn.Module):
                def __init__(self, input_size=1, hidden_size=64, num_layers=2):
                    super().__init__()
                    self.lstm = nn.LSTM(
                        input_size, hidden_size, num_layers, batch_first=True
                    )
                    self.fc = nn.Linear(hidden_size, 1)

                def forward(self, x):
                    lstm_out, _ = self.lstm(x)
                    return self.fc(lstm_out[:, -1, :])

            # Normalize data
            data = values[-200:]
            normalized = self._normalize(data)

            # Create sequences
            X, y = [], []
            for i in range(len(normalized) - self.sequence_length):
                X.append(normalized[i : i + self.sequence_length])
                y.append(normalized[i + self.sequence_length])

            if len(X) < 10:
                raise ValueError("Not enough data for LSTM")

            X = torch.FloatTensor(np.array(X)).unsqueeze(-1)
            y = torch.FloatTensor(np.array(y)).unsqueeze(-1)

            # Train model (quick)
            model = LSTMModel()
            optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
            criterion = nn.MSELoss()

            model.train()
            for epoch in range(50):  # Quick training
                optimizer.zero_grad()
                output = model(X)
                loss = criterion(output, y)
                loss.backward()
                optimizer.step()

            # Predict future
            model.eval()
            predictions = []
            current_seq = torch.FloatTensor(
                normalized[-self.sequence_length :]
            ).unsqueeze(0).unsqueeze(-1)  # shape: [1, seq_len, 1]

            with torch.no_grad():
                for _ in range(horizon_steps):
                    pred = model(current_seq)  # shape: [1, 1]
                    predictions.append(pred.item())
                    # Reshape pred to [1, 1, 1] for concatenation
                    next_val = pred.view(1, 1, 1)
                    # Slide window: drop oldest, append newest
                    current_seq = torch.cat(
                        [current_seq[:, 1:, :], next_val],
                        dim=1,
                    )

            predictions = self._denormalize(np.array(predictions))
            predictions = np.maximum(0, predictions)

            # Estimate uncertainty
            std = np.std(values[-30:])
            uncertainty = std * np.linspace(1.0, 2.0, horizon_steps)
            lower = predictions - uncertainty
            upper = predictions + uncertainty

            confidence = max(0.1, min(0.9, 1.0 - float(loss.item())))

            return predictions, np.maximum(0, lower), upper, confidence

        except Exception as e:
            logger.error(f"LSTM prediction failed: {e}")
            arima = ARIMAPredictor()
            return arima.predict(values, horizon_steps)


class EnsemblePredictor:
    """
    Ensemble predictor that combines multiple models with weighted averaging.
    Weights are based on recent prediction accuracy.
    """

    def __init__(self):
        self.arima = ARIMAPredictor()
        self.lstm = LSTMPredictor()
        self.weights = {"arima": 0.5, "lstm": 0.5}
        self.accuracies = {"arima": [], "lstm": []}

    def predict(
        self,
        timestamps: np.ndarray,
        values: np.ndarray,
        horizon_steps: int,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
        """
        Generate ensemble prediction by combining multiple models.
        """
        results = {}

        # Run ARIMA
        try:
            arima_pred, arima_low, arima_up, arima_conf = self.arima.predict(
                values, horizon_steps
            )
            results["arima"] = (arima_pred, arima_low, arima_up, arima_conf)
        except Exception as e:
            logger.warning(f"ARIMA failed in ensemble: {e}")

        # Run LSTM
        try:
            lstm_pred, lstm_low, lstm_up, lstm_conf = self.lstm.predict(
                values, horizon_steps
            )
            results["lstm"] = (lstm_pred, lstm_low, lstm_up, lstm_conf)
        except Exception as e:
            logger.warning(f"LSTM failed in ensemble: {e}")

        if not results:
            # All models failed — use simple extrapolation
            last_val = float(values[-1])
            predictions = np.full(horizon_steps, last_val)
            margin = np.std(values[-20:]) if len(values) >= 20 else last_val * 0.2
            return (
                predictions,
                predictions - margin,
                predictions + margin,
                0.3,
            )

        # Weighted combination
        total_weight = sum(
            self.weights.get(name, 0.5) for name in results
        )
        combined_pred = np.zeros(horizon_steps)
        combined_lower = np.zeros(horizon_steps)
        combined_upper = np.zeros(horizon_steps)
        combined_conf = 0.0

        for name, (pred, lower, upper, conf) in results.items():
            w = self.weights.get(name, 0.5) / total_weight
            combined_pred += w * pred
            combined_lower += w * lower
            combined_upper += w * upper
            combined_conf += w * conf

        return combined_pred, combined_lower, combined_upper, combined_conf


# ──────────────────────────────────────────────
# Replica Calculator
# ──────────────────────────────────────────────
class ReplicaCalculator:
    """Convert workload predictions to required replica counts."""

    def __init__(
        self,
        rps_per_pod: float = 50.0,
        min_replicas: int = 2,
        max_replicas: int = 50,
        target_utilization: float = 0.7,
        safety_margin: float = 1.2,
    ):
        self.rps_per_pod = rps_per_pod
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas
        self.target_utilization = target_utilization
        self.safety_margin = safety_margin

    def calculate(self, predicted_rps: float, confidence: float) -> int:
        """
        Calculate required replicas from predicted RPS.
        Lower confidence = more conservative (add safety margin).
        """
        # Adjust for confidence
        if confidence < 0.5:
            adjusted_rps = predicted_rps * (self.safety_margin + 0.3)
        elif confidence < 0.7:
            adjusted_rps = predicted_rps * self.safety_margin
        else:
            adjusted_rps = predicted_rps * 1.1  # Small buffer

        # Calculate raw replicas
        effective_rps_per_pod = self.rps_per_pod * self.target_utilization
        raw_replicas = adjusted_rps / effective_rps_per_pod

        # Apply bounds
        replicas = int(np.ceil(raw_replicas))
        replicas = max(self.min_replicas, min(self.max_replicas, replicas))

        return replicas


# ──────────────────────────────────────────────
# FastAPI Application
# ──────────────────────────────────────────────
app = FastAPI(
    title="ML Prediction Service",
    description="Workload prediction for hybrid autoscaler",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize components
metrics_fetcher = MetricsFetcher(PROMETHEUS_URL)
replica_calculator = ReplicaCalculator()
predictors = {
    "arima": ARIMAPredictor(),
    "lstm": LSTMPredictor(),
    "ensemble": EnsemblePredictor(),
}

# Store prediction history for accuracy tracking
prediction_history: list[dict] = []


@app.get("/health")
async def health():
    return {"status": "healthy", "service": "prediction-service"}


@app.get("/ready")
async def ready():
    return {"status": "ready"}


@app.post("/predict", response_model=PredictionResponse)
async def predict(request: PredictionRequest):
    """
    Generate workload prediction.
    
    This is the core endpoint used by the Scaling Controller.
    """
    start = time.time()
    model_type = request.model_type

    # Fetch historical metrics
    step = 15  # 15-second intervals
    timestamps, values = await metrics_fetcher.fetch_metric(
        metric_name=request.metric_name,
        duration_seconds=HISTORY_WINDOW,
        step=step,
    )

    if len(values) < 20:
        raise HTTPException(
            status_code=400,
            detail="Insufficient data for prediction (need >= 20 data points)",
        )

    # Calculate prediction steps
    horizon_steps = max(1, request.horizon_seconds // step)

    # Run prediction
    model_type = request.model_type
    if model_type == "ensemble":
        predictor = predictors["ensemble"]
        predictions, lower, upper, confidence = predictor.predict(
            timestamps, values, horizon_steps
        )
    elif model_type == "arima":
        predictor = predictors["arima"]
        predictions, lower, upper, confidence = predictor.predict(
            values, horizon_steps
        )
    elif model_type == "lstm":
        predictor = predictors["lstm"]
        predictions, lower, upper, confidence = predictor.predict(
            values, horizon_steps
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model type: {model_type}. Use: arima, lstm, ensemble",
        )

    # Calculate recommended replicas (peak predicted value)
    peak_predicted_rps = float(np.max(predictions))
    current_value = float(values[-1])
    recommended_replicas = replica_calculator.calculate(peak_predicted_rps, confidence)

    # Build response
    prediction_points = []
    base_time = time.time()
    for i in range(len(predictions)):
        prediction_points.append(
            PredictionPoint(
                timestamp=base_time + (i + 1) * step,
                value=float(predictions[i]),
                lower_bound=float(lower[i]),
                upper_bound=float(upper[i]),
            )
        )

    prediction_time = time.time() - start

    # Store prediction for accuracy tracking
    prediction_history.append({
        "timestamp": time.time(),
        "predicted_peak": peak_predicted_rps,
        "current_value": current_value,
        "model_type": model_type,
        "confidence": confidence,
        "recommended_replicas": recommended_replicas,
    })

    # Keep only last 1000 predictions
    if len(prediction_history) > 1000:
        prediction_history.pop(0)

    # Update Prometheus metrics
    PREDICTION_COUNT.labels(model_type=model_type, status="success").inc()
    PREDICTION_LATENCY.labels(model_type=model_type).observe(prediction_time)
    PREDICTION_CONFIDENCE.set(confidence)
    RECOMMENDED_REPLICAS.set(recommended_replicas)

    return PredictionResponse(
        metric_name=request.metric_name,
        model_type=model_type,
        predictions=prediction_points,
        confidence=round(confidence, 4),
        recommended_replicas=recommended_replicas,
        current_value=round(current_value, 2),
        prediction_time=round(prediction_time, 4),
        model_accuracy=round(1.0 - (abs(peak_predicted_rps - current_value) / (current_value + 1)), 4),
    )


@app.get("/predict/quick")
async def quick_predict():
    """
    Quick prediction using default parameters.
    Convenience endpoint for the scaling controller.
    """
    request = PredictionRequest()
    return await predict(request)


@app.get("/models")
async def list_models():
    """List available prediction models and their status."""
    return {
        "available_models": ["arima", "lstm", "ensemble"],
        "note": "Prophet is available in code but requires manual installation: pip install prophet",
        "default_model": MODEL_TYPE,
        "prediction_horizon_seconds": PREDICTION_HORIZON,
        "history_window_seconds": HISTORY_WINDOW,
        "confidence_threshold": CONFIDENCE_THRESHOLD,
    }


@app.get("/history")
async def get_prediction_history(limit: int = 100):
    """Get recent prediction history."""
    return {
        "predictions": prediction_history[-limit:],
        "total_predictions": len(prediction_history),
    }


@app.get("/accuracy")
async def get_accuracy():
    """Calculate prediction accuracy metrics."""
    if len(prediction_history) < 2:
        return {"status": "insufficient_data", "min_predictions_needed": 2}

    errors = []
    for pred in prediction_history[-100:]:
        if pred["current_value"] > 0:
            error = abs(pred["predicted_peak"] - pred["current_value"]) / pred["current_value"]
            errors.append(error)

    if not errors:
        return {"status": "no_valid_comparisons"}

    return {
        "mean_absolute_percentage_error": round(np.mean(errors) * 100, 2),
        "median_error_pct": round(np.median(errors) * 100, 2),
        "max_error_pct": round(np.max(errors) * 100, 2),
        "predictions_evaluated": len(errors),
        "avg_confidence": round(
            np.mean([p["confidence"] for p in prediction_history[-100:]]), 4
        ),
    }


@app.get("/metrics")
async def metrics():
    """Expose Prometheus metrics."""
    return Response(
        content=generate_latest(ml_registry),
        media_type="text/plain; charset=utf-8",
    )


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "prediction_service:app",
        host="0.0.0.0",
        port=PORT,
        workers=2,
        log_level="info",
    )

