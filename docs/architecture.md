# Architecture: Hybrid Predictive AutoScaler

## Overview

The Hybrid Predictive AutoScaler combines **ML-based proactive scaling** with **Kubernetes HPA reactive scaling** to eliminate cold-start latency during traffic spikes.

```
┌──────────────────────────────────────────────────────────────────┐
│                    Docker Compose / Kubernetes                     │
│                                                                    │
│  ┌─────────────┐    metrics    ┌──────────────┐                   │
│  │  App Service│◄──────────────│  Prometheus  │                   │
│  │  (FastAPI)  │               │   :9090      │                   │
│  │   :8000     │               └──────┬───────┘                   │
│  └──────┬──────┘                      │ query                     │
│         │                    ┌────────▼────────┐                  │
│         │ traffic            │   ML Service    │                  │
│         │                    │ ARIMA+LSTM+     │                  │
│  ┌──────▼──────┐             │ Ensemble :8001  │                  │
│  │  Traffic    │             └────────┬────────┘                  │
│  │  Generator  │                      │ /predict                  │
│  │  (Locust)   │             ┌────────▼────────┐                  │
│  └─────────────┘             │Scaling Controller│                 │
│                              │ Hybrid Logic     │                  │
│  ┌─────────────┐             │ Anti-flapping    │                  │
│  │   Grafana   │             │ Dry-run mode     │                  │
│  │   :3000     │             │ :8002            │                  │
│  └─────────────┘             └────────┬────────┘                  │
│                                       │ scale                     │
│  ┌─────────────┐             ┌────────▼────────┐                  │
│  │Web Dashboard│             │  Kubernetes API  │                  │
│  │   :8080     │             │  (or Dry Run)    │                  │
│  └─────────────┘             └─────────────────┘                  │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### 1. App Service (`app/`) — Port 8000
FastAPI application that simulates a workload. Exposes:
- `GET /` — root endpoint with simulated processing
- `GET /heavy` — CPU-intensive endpoint
- `GET /health` — health probe
- `GET /metrics` — Prometheus metrics (request count, latency histograms)

### 2. ML Prediction Service (`ml-service/`) — Port 8001
Multi-model time series forecaster. Supports three model types:

| Model | Description | Best For |
|---|---|---|
| **ARIMA** | Statistical autoregressive model | Stable, linear trends |
| **LSTM** | PyTorch deep learning | Non-linear, complex patterns |
| **Ensemble** | Weighted ARIMA+LSTM blend | **Production default** |

**Key Endpoints:**
- `POST /predict` — generate prediction (body: `{model_type, horizon_seconds}`)
- `GET /predict/quick` — quick prediction with defaults
- `GET /models` — list available models
- `GET /accuracy` — MAPE/RMSE accuracy metrics
- `GET /history` — recent prediction log

When Prometheus is unavailable, the service generates **synthetic data** anchored to actual traffic.

### 3. Scaling Controller (`controller/`) — Port 8002
Hybrid scaling algorithm with safety mechanisms:

```python
# Decision logic (simplified)
if confidence >= HIGH_THRESHOLD (0.6):
    target = 0.7 * predicted_replicas + 0.3 * current_replicas
elif confidence >= 0.4:
    target = average(current, predicted)   # conservative
else:
    target = current_replicas              # defer to HPA
```

**Safety Mechanisms:**
| Mechanism | Value | Purpose |
|---|---|---|
| Scale-up cooldown | 60s | Prevent rapid oscillation |
| Scale-down cooldown | 300s | Conservative reduction |
| Max step size | 5 pods | Rate-limit large jumps |
| Anti-flapping | 3 consecutive | Detect oscillation loops |
| Dry-run mode | `DRY_RUN=true` | Simulate without scaling |

### 4. Traffic Generator (`traffic-generator/`)
Locust-based load generator with `SpikyLoadShape` — simulates normal → gradual ramp → sudden spike traffic patterns.

### 5. Monitoring Stack
- **Prometheus** — scrapes metrics from all services every 15s
- **Grafana** — auto-provisioned dashboard (no manual setup needed)
- **Alert Rules** — fires on high latency, error rate, service down

## Hybrid Scaling Logic

```
Proactive (Predictive)          Reactive (HPA)
        │                              │
        ▼                              ▼
   ML forecasts RPS          CPU utilization threshold
   5 minutes ahead            triggers replica change
        │                              │
        └──────────┬───────────────────┘
                   ▼
           Hybrid Decision
        (weighted blend or fallback)
                   │
                   ▼
           Safety Checks
     (cooldowns, rate limits, bounds)
                   │
                   ▼
        Scale Deployment / No-op
```

**Key insight:** The controller **only scales up** proactively. Scale-down is deferred to HPA (or the longer 300s cooldown) to avoid premature reduction during transient lulls.

## Data Flow

```
1. Traffic Generator → sends requests → App Service
2. App Service → exposes metrics → Prometheus scrapes
3. ML Service → queries Prometheus → trains ARIMA+LSTM → makes prediction
4. Scaling Controller → polls ML Service every 30s → computes hybrid target
5. Controller → patches K8s Deployment replicas (or logs in dry-run)
6. HPA → independently monitors CPU → provides reactive safety net
7. Grafana → visualizes all metrics → operator sees full picture
```

## Local Development

```bash
# Start full stack
docker compose up -d --build

# Check all services
curl http://localhost:8000/health      # App
curl http://localhost:8001/health      # ML
curl http://localhost:8002/health      # Controller
curl http://localhost:8001/predict/quick   # Quick prediction

# Watch scaling decisions
docker compose logs -f scaling-controller

# Stop
docker compose down
```

## Kubernetes Deployment

```bash
# Using kubectl
kubectl apply -f k8s-manifests/

# Using Helm
helm install hybrid-autoscaler helm/hybrid-autoscaler/ \
  --namespace hybrid-autoscaler --create-namespace \
  --set scalingController.config.dryRun=false
```
