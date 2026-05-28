# Hybrid Predictive AutoScaler

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Sandeep17022005/hybrid-predictive-autoscaler)
[![CI/CD](https://github.com/Sandeep17022005/hybrid-predictive-autoscaler/actions/workflows/ci-cd.yaml/badge.svg)](https://github.com/Sandeep17022005/hybrid-predictive-autoscaler/actions)

> **Live Demo Endpoints** (deployed on Render.com free tier)
> - 🚀 **App Service:** https://hpa-app-service.onrender.com
> - 🧠 **ML Prediction API:** https://hpa-ml-service.onrender.com/predict
> - 📖 **API Docs (App):** https://hpa-app-service.onrender.com/docs
> - 📖 **API Docs (ML):** https://hpa-ml-service.onrender.com/docs

An intelligent Kubernetes autoscaling system that combines **ML-based predictive scaling** (ARIMA + LSTM + Ensemble) with **Kubernetes HPA reactive scaling** — eliminating cold-start latency during traffic spikes while minimising resource waste.

---

## ⚡ Quick Start — Docker Compose (Local)

**Run the full stack locally in one command:**

```bash
git clone https://github.com/Sandeep17022005/hybrid-predictive-autoscaler.git
cd hybrid-predictive-autoscaler
docker compose up -d --build
```

| Service | URL | Description |
|---|---|---|
| 🚀 App Service | http://localhost:8000 | FastAPI workload + metrics |
| 🧠 ML Service | http://localhost:8001/docs | ARIMA+LSTM+Ensemble prediction |
| ⚙️ Controller Status | http://localhost:8002/status | Hybrid scaler status |
| 📊 Prometheus | http://localhost:9090 | Metrics collection |
| 📈 Grafana | http://localhost:3000 | Auto-provisioned dashboard (admin/admin) |
| 🖥️ Web Dashboard | http://localhost:8080 | Live prediction dashboard |

```bash
# Test the ML prediction API
curl -X POST http://localhost:8001/predict \
  -H "Content-Type: application/json" \
  -d '{"model_type": "ensemble", "horizon_seconds": 300}'

# Check controller status
curl http://localhost:8002/status

# View scaling logs
docker compose logs -f scaling-controller

# Stop everything
docker compose down
```

---

## 🏗️ Architecture

```
Traffic Generator → App Service → Prometheus
                                      ↓
                              ML Service (ARIMA+LSTM+Ensemble)
                                      ↓ /predict
                              Scaling Controller
                           (Hybrid: Predictive + Reactive)
                                      ↓
                         Kubernetes Deployment Scale API
                         (or Dry-run log in local mode)
```

See [docs/architecture.md](docs/architecture.md) for full data flow and component details.

---

## 🧠 ML Models

| Model | Algorithm | Best For |
|---|---|---|
| **ARIMA** | Statistical autoregressive | Stable, linear traffic |
| **LSTM** | PyTorch deep learning | Non-linear complex patterns |
| **Ensemble** ⭐ | Weighted ARIMA+LSTM blend | **Production default** |

Switch models per-request:
```bash
curl -X POST http://localhost:8001/predict \
  -d '{"model_type": "arima"}'   # or "lstm" or "ensemble"
```

---

## ⚙️ Hybrid Scaling Algorithm

```python
if confidence >= 0.6:       # HIGH — trust prediction
    target = 0.7 * predicted_replicas + 0.3 * current_replicas
elif confidence >= 0.4:     # MEDIUM — conservative blend
    target = average(current, predicted)
else:                       # LOW — defer to HPA
    target = current_replicas
```

**Safety mechanisms:** cooldowns (60s up / 300s down), max step size (5 pods), anti-flapping (3 consecutive), dry-run mode.

---

## 📡 API Reference

### ML Service (`:8001`)
| Endpoint | Method | Description |
|---|---|---|
| `/predict` | POST | Generate prediction (ARIMA/LSTM/Ensemble) |
| `/predict/quick` | GET | Quick prediction with defaults |
| `/models` | GET | List available models |
| `/accuracy` | GET | MAPE/RMSE accuracy metrics |
| `/history` | GET | Recent prediction log |
| `/health` | GET | Health check |
| `/metrics` | GET | Prometheus metrics |

### Controller (`:8002`)
| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Health check |
| `/status` | GET | Config + scaling state |
| `/metrics` | GET | Prometheus metrics |

---

## ☸️ Kubernetes Deployment

### Using kubectl
```bash
kubectl apply -f k8s-manifests/
kubectl get pods -n default
kubectl get hpa
```

### Using Helm
```bash
helm install hybrid-autoscaler helm/hybrid-autoscaler/ \
  --namespace hybrid-autoscaler \
  --create-namespace \
  --set scalingController.config.dryRun=false \
  --wait
```

### Prerequisites
- Docker 24+
- Minikube / kind / any Kubernetes cluster
- `kubectl` configured
- Helm 3.x (optional)

---

## 📊 Monitoring

- **Grafana dashboard** auto-provisions on startup (no manual setup)
- **Prometheus alert rules** fire on: high latency, high error rate, service down, traffic spike
- **Web dashboard** at `localhost:8080` shows live ML predictions

---

## 📁 Project Structure

```
hybrid-predictive-autoscaler/
├── app/                    # FastAPI workload service
├── ml-service/             # ARIMA + LSTM + Ensemble prediction
├── controller/             # Hybrid scaling controller
├── traffic-generator/      # Locust load generator
├── dashboard/              # Web dashboard (served by nginx)
├── dashboards/grafana/     # Grafana dashboard + auto-provisioning
├── monitoring/prometheus/  # Prometheus config + alert rules
├── k8s-manifests/          # Kubernetes manifests
├── helm/                   # Helm chart
├── docs/                   # Architecture documentation
├── .github/workflows/      # GitHub Actions CI/CD
├── docker-compose.yml      # Full local stack
└── render.yaml             # Render.com deployment
```

---

## 🔧 Configuration

Key environment variables for the Scaling Controller:

| Variable | Default | Description |
|---|---|---|
| `DRY_RUN` | `true` | Simulate scaling (set `false` for real scaling) |
| `MIN_REPLICAS` | `2` | Minimum pod count |
| `MAX_REPLICAS` | `50` | Maximum pod count |
| `CONFIDENCE_THRESHOLD` | `0.6` | Min confidence to trust prediction |
| `SCALE_UP_COOLDOWN` | `60` | Seconds between scale-ups |
| `SCALE_DOWN_COOLDOWN` | `300` | Seconds between scale-downs |
| `POLL_INTERVAL` | `30` | Seconds between scaling checks |

---

## ⚠️ Recent Fixes (v1.0)

This codebase was analyzed and fixed for 37 issues including:
- ✅ Race conditions and thread safety (asyncio.Lock)
- ✅ Impossible confidence threshold logic
- ✅ Missing health probes, RBAC, resource limits
- ✅ Non-root container users (security hardening)
- ✅ Graceful shutdown signal handling
- ✅ Anti-flapping logic in controller
- ✅ Structured logging throughout

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.
