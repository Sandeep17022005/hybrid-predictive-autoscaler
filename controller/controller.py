"""
Hybrid Predictive Autoscaler - Scaling Controller
===================================================
Custom Kubernetes controller that implements hybrid scaling logic:
  1. Fetches predictions from ML service
  2. Reads current HPA state
  3. Applies hybrid scaling algorithm
  4. Scales deployments via Kubernetes API
  5. Enforces safety mechanisms (cooldowns, bounds, rate limiting)

This controller runs as a Deployment inside the cluster.
"""

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import httpx
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
PREDICTION_SERVICE_URL = os.getenv(
    "PREDICTION_SERVICE_URL", "http://prediction-service:8001"
)
NAMESPACE = os.getenv("NAMESPACE", "default")
TARGET_DEPLOYMENT = os.getenv("TARGET_DEPLOYMENT", "workload-service")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))  # seconds
MIN_REPLICAS = int(os.getenv("MIN_REPLICAS", "2"))
MAX_REPLICAS = int(os.getenv("MAX_REPLICAS", "50"))
SCALE_UP_COOLDOWN = int(os.getenv("SCALE_UP_COOLDOWN", "60"))  # seconds
SCALE_DOWN_COOLDOWN = int(os.getenv("SCALE_DOWN_COOLDOWN", "300"))  # seconds
MAX_SCALE_STEP = int(os.getenv("MAX_SCALE_STEP", "5"))  # max pods per scaling event
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.6"))
PREDICTION_WEIGHT = float(os.getenv("PREDICTION_WEIGHT", "0.7"))
REACTIVE_WEIGHT = float(os.getenv("REACTIVE_WEIGHT", "0.3"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scaling-controller")

# ──────────────────────────────────────────────
# Prometheus Metrics
# ──────────────────────────────────────────────
ctrl_registry = CollectorRegistry()

SCALING_EVENTS_TOTAL = Counter(
    "controller_scaling_events_total",
    "Total scaling events",
    ["decision", "source"],
    registry=ctrl_registry,
)

CURRENT_REPLICAS_GAUGE = Gauge(
    "controller_current_replicas",
    "Current replica count",
    registry=ctrl_registry,
)

PREDICTED_RPS_GAUGE = Gauge(
    "controller_predicted_rps",
    "Latest predicted RPS from ML service",
    registry=ctrl_registry,
)

CONFIDENCE_GAUGE = Gauge(
    "controller_prediction_confidence",
    "Latest prediction confidence used for scaling",
    registry=ctrl_registry,
)


# ──────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────
class ScalingDecision(Enum):
    SCALE_UP = "scale_up"
    SCALE_DOWN = "scale_down"
    NO_CHANGE = "no_change"
    FALLBACK_REACTIVE = "fallback_reactive"


@dataclass
class ScalingEvent:
    timestamp: float
    decision: ScalingDecision
    source: str  # "predictive", "reactive", "hybrid"
    current_replicas: int
    target_replicas: int
    predicted_rps: float
    confidence: float
    reason: str


@dataclass
class ScalingState:
    current_replicas: int = MIN_REPLICAS
    last_scale_up_time: float = 0.0
    last_scale_down_time: float = 0.0
    consecutive_scale_ups: int = 0
    consecutive_scale_downs: int = 0
    events: list[ScalingEvent] = field(default_factory=list)
    total_scale_ups: int = 0
    total_scale_downs: int = 0


# ──────────────────────────────────────────────
# Kubernetes Client
# ──────────────────────────────────────────────
class KubernetesClient:
    """
    Kubernetes API client for scaling operations.
    Uses in-cluster config when running inside K8s,
    falls back to kubeconfig for local development.
    """

    def __init__(self):
        self.in_cluster = os.path.exists(
            "/var/run/secrets/kubernetes.io/serviceaccount/token"
        )
        self.api_base = ""
        self.headers = {}
        self._setup()

    def _setup(self):
        if self.in_cluster:
            self.api_base = "https://kubernetes.default.svc"
            token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
            with open(token_path) as f:
                token = f.read().strip()
            self.headers = {"Authorization": f"Bearer {token}"}
            logger.info("Using in-cluster Kubernetes configuration")
        else:
            self.api_base = "http://localhost:8001"  # kubectl proxy
            logger.info("Using kubectl proxy (localhost:8001)")

    async def get_deployment(
        self, name: str, namespace: str
    ) -> Optional[dict]:
        """Get deployment details."""
        url = (
            f"{self.api_base}/apis/apps/v1"
            f"/namespaces/{namespace}/deployments/{name}"
        )
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=10.0
            ) as client:
                response = await client.get(url, headers=self.headers)
                if response.status_code == 200:
                    return response.json()
                logger.error(f"Get deployment failed: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Failed to get deployment: {e}")
            return None

    async def scale_deployment(
        self, name: str, namespace: str, replicas: int
    ) -> bool:
        """Scale a deployment to the specified replica count."""
        url = (
            f"{self.api_base}/apis/apps/v1"
            f"/namespaces/{namespace}/deployments/{name}/scale"
        )
        scale_body = {
            "apiVersion": "autoscaling/v1",
            "kind": "Scale",
            "metadata": {"name": name, "namespace": namespace},
            "spec": {"replicas": replicas},
        }

        try:
            async with httpx.AsyncClient(
                verify=False, timeout=10.0
            ) as client:
                response = await client.put(
                    url,
                    json=scale_body,
                    headers={**self.headers, "Content-Type": "application/json"},
                )
                if response.status_code in (200, 201):
                    logger.info(
                        f"[OK] Scaled {name} to {replicas} replicas"
                    )
                    return True
                logger.error(
                    f"Scale failed: {response.status_code} - {response.text}"
                )
                return False
        except Exception as e:
            logger.error(f"Failed to scale deployment: {e}")
            return False

    async def get_current_replicas(
        self, name: str, namespace: str
    ) -> int:
        """Get current replica count for a deployment."""
        deployment = await self.get_deployment(name, namespace)
        if deployment:
            return deployment.get("spec", {}).get("replicas", MIN_REPLICAS)
        return MIN_REPLICAS

    async def get_hpa(
        self, name: str, namespace: str
    ) -> Optional[dict]:
        """Get HPA details."""
        url = (
            f"{self.api_base}/apis/autoscaling/v2"
            f"/namespaces/{namespace}/horizontalpodautoscalers/{name}"
        )
        try:
            async with httpx.AsyncClient(
                verify=False, timeout=10.0
            ) as client:
                response = await client.get(url, headers=self.headers)
                if response.status_code == 200:
                    return response.json()
                return None
        except Exception as e:
            logger.warning(f"Failed to get HPA: {e}")
            return None


# ──────────────────────────────────────────────
# Prediction Client
# ──────────────────────────────────────────────
class PredictionClient:
    """Client for the ML Prediction Service."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def get_prediction(self) -> Optional[dict]:
        """Fetch prediction from ML service."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.base_url}/predict",
                    json={
                        "metric_name": "app_request_rate_per_second",
                        "horizon_seconds": 300,
                        "model_type": "ensemble",
                    },
                )
                if response.status_code == 200:
                    return response.json()
                logger.error(
                    f"Prediction request failed: {response.status_code}"
                )
                return None
        except Exception as e:
            logger.error(f"Failed to get prediction: {e}")
            return None


# ──────────────────────────────────────────────
# Hybrid Scaling Algorithm
# ──────────────────────────────────────────────
class HybridScaler:
    """
    Implements the hybrid scaling algorithm that combines:
    - Predictive scaling (proactive, ML-based)
    - Reactive scaling (HPA-based, safety layer)
    
    Algorithm:
    1. Get prediction from ML service
    2. Check prediction confidence
    3. If high confidence: use predictive scaling
    4. If low confidence: blend with reactive or fallback
    5. Apply safety mechanisms (cooldowns, rate limits, bounds)
    """

    def __init__(self):
        self.state = ScalingState()
        self.k8s_client = KubernetesClient()
        self.prediction_client = PredictionClient(PREDICTION_SERVICE_URL)

    def _apply_safety_checks(
        self,
        current_replicas: int,
        target_replicas: int,
        confidence: float,
    ) -> tuple[int, str]:
        """
        Apply safety mechanisms to prevent aggressive scaling.
        Returns (safe_target, reason).
        """
        now = time.time()

        # 1. Apply bounds
        target_replicas = max(MIN_REPLICAS, min(MAX_REPLICAS, target_replicas))

        # 2. Rate limiting — max step size
        delta = target_replicas - current_replicas
        if abs(delta) > MAX_SCALE_STEP:
            direction = 1 if delta > 0 else -1
            target_replicas = current_replicas + (direction * MAX_SCALE_STEP)
            logger.info(
                f"[WARN] Rate-limited scaling to step of {MAX_SCALE_STEP}"
            )

        # 3. Cooldown enforcement
        if target_replicas > current_replicas:
            # Scale-up cooldown
            if now - self.state.last_scale_up_time < SCALE_UP_COOLDOWN:
                remaining = SCALE_UP_COOLDOWN - (now - self.state.last_scale_up_time)
                return current_replicas, f"Scale-up cooldown active ({remaining:.0f}s remaining)"
        elif target_replicas < current_replicas:
            # Scale-down cooldown (longer to avoid flapping)
            if now - self.state.last_scale_down_time < SCALE_DOWN_COOLDOWN:
                remaining = SCALE_DOWN_COOLDOWN - (now - self.state.last_scale_down_time)
                return current_replicas, f"Scale-down cooldown active ({remaining:.0f}s remaining)"

        # 4. Confidence-based adjustment
        if confidence < 0.4:
            # Very low confidence: conservative approach
            if target_replicas > current_replicas:
                # Only allow small scale-up
                max_increase = max(1, int((target_replicas - current_replicas) * 0.5))
                target_replicas = current_replicas + max_increase
                return target_replicas, "Low confidence: conservative scale-up"
            else:
                # Don't scale down on low confidence
                return current_replicas, "Low confidence: holding current replicas"

        # 5. Anti-flapping: if we've been scaling up and down rapidly, pause
        if (
            self.state.consecutive_scale_ups >= 3
            and target_replicas < current_replicas
        ):
            return current_replicas, "Anti-flapping: too many consecutive scale-ups"
        if (
            self.state.consecutive_scale_downs >= 3
            and target_replicas > current_replicas
        ):
            self.state.consecutive_scale_downs = 0  # Reset, allow scale-up

        return target_replicas, "Safety checks passed"

    async def compute_scaling_decision(self) -> ScalingEvent:
        """
        Core hybrid scaling algorithm.
        
        Pseudocode:
        1. FETCH prediction from ML service
        2. READ current deployment state
        3. IF prediction confidence >= threshold:
              target = weighted_blend(predicted_replicas, hpa_replicas)
        4. ELSE:
              target = hpa_replicas  (fallback to reactive)
        5. APPLY safety checks (cooldowns, rate limits, bounds)
        6. EXECUTE scaling if target != current
        """
        now = time.time()

        # Get current state
        current_replicas = await self.k8s_client.get_current_replicas(
            TARGET_DEPLOYMENT, NAMESPACE
        )
        self.state.current_replicas = current_replicas

        # Get prediction
        prediction = await self.prediction_client.get_prediction()

        if prediction is None:
            # Prediction service unavailable — fallback to reactive
            return ScalingEvent(
                timestamp=now,
                decision=ScalingDecision.FALLBACK_REACTIVE,
                source="reactive",
                current_replicas=current_replicas,
                target_replicas=current_replicas,
                predicted_rps=0,
                confidence=0,
                reason="Prediction service unavailable — relying on HPA",
            )

        predicted_rps = max(
            p["value"] for p in prediction["predictions"]
        )
        confidence = prediction["confidence"]
        recommended_replicas = prediction["recommended_replicas"]

        # ── Hybrid Decision Logic ──
        if confidence >= CONFIDENCE_THRESHOLD:
            # High confidence: use predictive scaling with blend
            predictive_target = recommended_replicas
            reactive_target = current_replicas  # HPA will handle reactively

            # Weighted blend
            hybrid_target = int(
                PREDICTION_WEIGHT * predictive_target
                + REACTIVE_WEIGHT * reactive_target
            )
            source = "predictive"
            reason = (
                f"High confidence ({confidence:.2f}): "
                f"predicted={predictive_target}, blend={hybrid_target}"
            )
        elif confidence >= 0.4:
            # Medium confidence: conservative predictive
            predictive_target = recommended_replicas
            # Use average of current and predicted
            hybrid_target = int((current_replicas + predictive_target) / 2)
            source = "hybrid"
            reason = (
                f"Medium confidence ({confidence:.2f}): "
                f"conservative blend={hybrid_target}"
            )
        else:
            # Low confidence: fallback to reactive (let HPA handle it)
            hybrid_target = current_replicas
            source = "reactive"
            reason = (
                f"Low confidence ({confidence:.2f}): "
                f"fallback to HPA reactive scaling"
            )

        # Apply safety mechanisms
        safe_target, safety_reason = self._apply_safety_checks(
            current_replicas, hybrid_target, confidence
        )

        if safe_target != hybrid_target:
            reason += f" | Safety: {safety_reason}"
            hybrid_target = safe_target

        # Determine decision type
        if hybrid_target > current_replicas:
            decision = ScalingDecision.SCALE_UP
        elif hybrid_target < current_replicas:
            decision = ScalingDecision.SCALE_DOWN
        else:
            decision = ScalingDecision.NO_CHANGE

        event = ScalingEvent(
            timestamp=now,
            decision=decision,
            source=source,
            current_replicas=current_replicas,
            target_replicas=hybrid_target,
            predicted_rps=predicted_rps,
            confidence=confidence,
            reason=reason,
        )

        return event

    async def execute_scaling(self, event: ScalingEvent) -> bool:
        """Execute scaling decision."""
        if event.decision == ScalingDecision.NO_CHANGE:
            logger.info(
                f"[HOLD] No scaling needed: {event.current_replicas} replicas | {event.reason}"
            )
            return True

        if event.decision == ScalingDecision.FALLBACK_REACTIVE:
            logger.warning(f"[FALLBACK] Fallback to reactive: {event.reason}")
            return True

        logger.info(
            f"{'[UP]' if event.decision == ScalingDecision.SCALE_UP else '[DOWN]'} "
            f"Scaling {event.decision.value}: "
            f"{event.current_replicas} → {event.target_replicas} "
            f"(source: {event.source}, confidence: {event.confidence:.2f})"
        )
        logger.info(f"   Reason: {event.reason}")

        if DRY_RUN:
            logger.info("[DRY] DRY RUN -- skipping actual scaling")
            success = True
        else:
            success = await self.k8s_client.scale_deployment(
                TARGET_DEPLOYMENT, NAMESPACE, event.target_replicas
            )

        if success:
            now = time.time()
            if event.decision == ScalingDecision.SCALE_UP:
                self.state.last_scale_up_time = now
                self.state.consecutive_scale_ups += 1
                self.state.consecutive_scale_downs = 0
                self.state.total_scale_ups += 1
            elif event.decision == ScalingDecision.SCALE_DOWN:
                self.state.last_scale_down_time = now
                self.state.consecutive_scale_downs += 1
                self.state.consecutive_scale_ups = 0
                self.state.total_scale_downs += 1

            self.state.current_replicas = event.target_replicas

        # Update Prometheus metrics
        SCALING_EVENTS_TOTAL.labels(
            decision=event.decision.value, source=event.source
        ).inc()
        CURRENT_REPLICAS_GAUGE.set(event.target_replicas)
        PREDICTED_RPS_GAUGE.set(event.predicted_rps)
        CONFIDENCE_GAUGE.set(event.confidence)

        self.state.events.append(event)
        # Keep last 500 events
        if len(self.state.events) > 500:
            self.state.events = self.state.events[-500:]

        return success


# ──────────────────────────────────────────────
# Controller Loop
# ──────────────────────────────────────────────
async def controller_loop(shutdown_event: asyncio.Event):
    """Main control loop — runs continuously until shutdown."""
    scaler = HybridScaler()

    logger.info("=" * 60)
    logger.info("[CTRL] Hybrid Predictive Scaling Controller Starting")
    logger.info(f"   Target: {TARGET_DEPLOYMENT}")
    logger.info(f"   Namespace: {NAMESPACE}")
    logger.info(f"   Poll interval: {POLL_INTERVAL}s")
    logger.info(f"   Confidence threshold: {CONFIDENCE_THRESHOLD}")
    logger.info(f"   Weights: predictive={PREDICTION_WEIGHT}, reactive={REACTIVE_WEIGHT}")
    logger.info(f"   Bounds: [{MIN_REPLICAS}, {MAX_REPLICAS}]")
    logger.info(f"   Dry run: {DRY_RUN}")
    logger.info("=" * 60)

    iteration = 0
    while not shutdown_event.is_set():
        iteration += 1
        try:
            logger.info(f"\n{'─' * 40} Iteration {iteration} {'─' * 40}")

            # Compute scaling decision
            event = await scaler.compute_scaling_decision()

            # Execute scaling
            await scaler.execute_scaling(event)

            # Log state summary
            state = scaler.state
            logger.info(
                f"[STATE] State: replicas={state.current_replicas}, "
                f"total_ups={state.total_scale_ups}, "
                f"total_downs={state.total_scale_downs}"
            )

        except Exception as e:
            logger.error(f"[ERROR] Controller error: {e}", exc_info=True)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=POLL_INTERVAL)
        except asyncio.TimeoutError:
            pass

    logger.info("[STOP] Controller loop shutting down gracefully")


# ──────────────────────────────────────────────
# REST API for Controller Status
# ──────────────────────────────────────────────
from fastapi import FastAPI, Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware

api = FastAPI(title="Scaling Controller API", version="1.0.0")

api.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/health")
async def health():
    return {"status": "healthy", "service": "scaling-controller"}


@api.get("/status")
async def status():
    return {
        "service": "scaling-controller",
        "target_deployment": TARGET_DEPLOYMENT,
        "namespace": NAMESPACE,
        "dry_run": DRY_RUN,
        "poll_interval": POLL_INTERVAL,
        "config": {
            "min_replicas": MIN_REPLICAS,
            "max_replicas": MAX_REPLICAS,
            "confidence_threshold": CONFIDENCE_THRESHOLD,
            "prediction_weight": PREDICTION_WEIGHT,
            "reactive_weight": REACTIVE_WEIGHT,
            "scale_up_cooldown": SCALE_UP_COOLDOWN,
            "scale_down_cooldown": SCALE_DOWN_COOLDOWN,
            "max_scale_step": MAX_SCALE_STEP,
        },
    }


@api.get("/metrics")
async def metrics():
    """Expose Prometheus metrics."""
    return FastAPIResponse(
        content=generate_latest(ctrl_registry),
        media_type="text/plain; charset=utf-8",
    )


# ──────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────
async def main():
    """Start both the controller loop and the status API."""
    import uvicorn

    shutdown_event = asyncio.Event()

    # Register signal handlers for graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: shutdown_event.set())

    config = uvicorn.Config(api, host="0.0.0.0", port=8002, log_level="warning")
    server = uvicorn.Server(config)

    # Run controller loop and API server concurrently
    await asyncio.gather(
        controller_loop(shutdown_event),
        server.serve(),
    )


if __name__ == "__main__":
    asyncio.run(main())

