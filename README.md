# Hybrid Predictive AutoScaler

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Sandeep17022005/hybrid-predictive-autoscaler)

> **Live Demo Endpoints** (deployed on Render.com free tier)
> - 🚀 **App Service:** https://hpa-app-service.onrender.com
> - 🧠 **ML Prediction API:** https://hpa-ml-service.onrender.com/predict
> - 📖 **API Docs (App):** https://hpa-app-service.onrender.com/docs
> - 📖 **API Docs (ML):** https://hpa-ml-service.onrender.com/docs

This project implements an intelligent autoscaling system for Kubernetes that combines machine learning-based predictive scaling with Kubernetes Horizontal Pod Autoscaler (HPA) to improve system performance, reduce latency during spikes, and minimize resource waste.

## ⚠️ Important: Recent Comprehensive Fixes

**This codebase has been analyzed and fixed for 37 major and minor issues** including:
- 🔴 Race conditions and thread safety (asyncio.Lock, threading.Lock)
- 🟠 Missing health probes, RBAC, and resource limits
- 🟡 Error handling, validation, and graceful shutdown
- 🔵 Code quality, logging, and security hardening

**See [FIXES.md](FIXES.md) for the comprehensive list of all issues and their resolutions.**

Key improvements:
- ✅ Fixed impossible confidence threshold logic (was blocking all predictions)
- ✅ Added health probes to all deployments
- ✅ Added Prometheus RBAC configuration
- ✅ Added comprehensive error handling and retries
- ✅ Versioned Docker images (v1.0.0) instead of :latest
- ✅ Non-root users in containers for security
- ✅ Structured logging throughout
- ✅ Environment variable validation with meaningful errors

## Project Architecture

1. **Application Service**: A FastAPI application exposing `/metrics` for Prometheus.
2. **Monitoring**: Prometheus scrapes metrics, Grafana visualizes them.
3. **ML Prediction Service**: An ARIMA-based time series model that forecasts Requests Per Second (RPS) 5 minutes into the future based on historical Prometheus data.
4. **Predictive Scaling Controller**: A custom Python Kubernetes operator that polls the ML Prediction Service and proactively scales the Application Service Deployment *up* before load increases.
5. **Reactive HPA**: A standard Kubernetes HPA serves as a fallback to scale the deployment based on real-time CPU utilization.
6. **Traffic Generator**: A Locust script simulating normal, gradual, and spiky traffic to test the hybrid algorithms.

## Prerequisites
- Docker
- Minikube
- `kubectl`

## Deployment Instructions

### 1. Start Minikube
Start a Minikube cluster with the metrics-server addon enabled (required for HPA).
```bash
minikube start --driver=docker --addons=metrics-server
```

### 2. Build Docker Images
Point your local Docker daemon to the Minikube docker daemon, then build the images.
```bash
eval $(minikube docker-env)

# Build App Service
docker build -t app-service:latest ./app

# Build ML Service
docker build -t ml-service:latest ./ml-service

# Build Predictive Controller
docker build -t scaling-controller:latest ./controller

# Build Traffic Generator
docker build -t traffic-generator:latest ./traffic-generator
```

### 3. Deploy the Stack
Deploy Prometheus and Grafana first:
```bash
kubectl apply -f k8s-manifests/monitoring/
```

Deploy the Application, ML Service, custom Scaling Controller, and HPA:
```bash
kubectl apply -f k8s-manifests/rbac.yaml
kubectl apply -f k8s-manifests/app-deployment.yaml
kubectl apply -f k8s-manifests/ml-service.yaml
kubectl apply -f k8s-manifests/controller.yaml
kubectl apply -f k8s-manifests/hpa.yaml
```

### 4. Verify Deployments
Ensure all pods are running successfully:
```bash
kubectl get pods
kubectl get hpa
```

### 5. Generate Traffic
To test the autoscaler, start the Traffic Generator deployment:
```bash
kubectl apply -f k8s-manifests/traffic-generator.yaml
```
The Locust script is configured with a `SpikyLoadShape` which simulates variable load over a few minutes.

### 6. Monitor
You can access Grafana to visualize metrics.
```bash
minikube service grafana-service
```
*(Default Grafana login is anonymous)*

You can also view the logs of the predictive controller to see its scaling decisions:
```bash
kubectl logs -l app=predictive-controller -f
```

## Hybrid Scaling Logic Details
- **Predictive (Proactive)**: The `predictive-controller` queries the ML API. If predicted RPS mandates more pods than currently exist, it patches the deployment to scale *up*. It never scales *down*.
- **Reactive (Corrective)**: The `HorizontalPodAutoscaler` continually monitors CPU. If the predictor fails to catch a spike, HPA scales up. When the traffic dies down, the HPA will eventually scale the deployment down (since the custom controller defers scale-down operations to the strict CPU metric).
