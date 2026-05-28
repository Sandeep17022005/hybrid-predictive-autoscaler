import os
import time
import requests
import logging
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from kubernetes import client, config

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration with validation
def validate_env_config():
    """Validate and parse environment variables with meaningful error messages."""
    try:
        ml_service_url = os.getenv("ML_SERVICE_URL", "http://ml-prediction-service:8000/predict")
        target_rps = float(os.getenv("TARGET_RPS_PER_POD", "50.0"))
        deployment_name = os.getenv("DEPLOYMENT_NAME", "app-deployment")
        namespace = os.getenv("NAMESPACE", "default")
        min_replicas = int(os.getenv("MIN_REPLICAS", "2"))
        max_replicas = int(os.getenv("MAX_REPLICAS", "10"))
        poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))
        max_scale_delta = int(os.getenv("MAX_SCALE_DELTA", "2"))
        scale_cooldown = int(os.getenv("SCALE_COOLDOWN_SECONDS", "60"))
        confidence_threshold = float(os.getenv("CONFIDENCE_THRESHOLD", "0.5"))
        
        # Validate ranges
        if min_replicas <= 0:
            raise ValueError(f"MIN_REPLICAS must be > 0, got {min_replicas}")
        if max_replicas < min_replicas:
            raise ValueError(f"MAX_REPLICAS ({max_replicas}) must be >= MIN_REPLICAS ({min_replicas})")
        if target_rps <= 0:
            raise ValueError(f"TARGET_RPS_PER_POD must be > 0 (division by zero risk), got {target_rps}")
        if poll_interval <= 0:
            raise ValueError(f"POLL_INTERVAL_SECONDS must be > 0, got {poll_interval}")
        if confidence_threshold < 0 or confidence_threshold > 1.0:
            raise ValueError(f"CONFIDENCE_THRESHOLD must be in [0.0, 1.0], got {confidence_threshold}")
        
        return {
            "ml_service_url": ml_service_url,
            "target_rps": target_rps,
            "deployment_name": deployment_name,
            "namespace": namespace,
            "min_replicas": min_replicas,
            "max_replicas": max_replicas,
            "poll_interval": poll_interval,
            "max_scale_delta": max_scale_delta,
            "scale_cooldown": scale_cooldown,
            "confidence_threshold": confidence_threshold,
        }
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        raise

config_vars = validate_env_config()
ML_SERVICE_URL = config_vars["ml_service_url"]
TARGET_RPS_PER_POD = config_vars["target_rps"]
DEPLOYMENT_NAME = config_vars["deployment_name"]
NAMESPACE = config_vars["namespace"]
MIN_REPLICAS = config_vars["min_replicas"]
MAX_REPLICAS = config_vars["max_replicas"]
POLL_INTERVAL_SECONDS = config_vars["poll_interval"]
MAX_SCALE_DELTA = config_vars["max_scale_delta"]
SCALE_COOLDOWN_SECONDS = config_vars["scale_cooldown"]
CONFIDENCE_THRESHOLD = config_vars["confidence_threshold"]

# Thread-safe state management
last_scale_ts_lock = threading.Lock()
last_scale_ts = 0

def create_session_with_retries():
    """Create a requests Session with configured retry strategy for resilience."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def main():
    """Main predictive scaling controller loop."""
    logger.info("Starting Predictive Scaling Controller...")
    logger.info(f"Configuration: Min={MIN_REPLICAS}, Max={MAX_REPLICAS}, TargetRPS={TARGET_RPS_PER_POD}, ConfidenceThreshold={CONFIDENCE_THRESHOLD}")
    
    # Load Kubernetes config depending on if we are running in-cluster or out
    try:
        config.load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes config.")
    except config.config_exception.ConfigException:
        logger.info("Falling back to local kube config.")
        try:
            config.load_kube_config()
            logger.info("Loaded local Kubernetes config.")
        except config.config_exception.ConfigException as e:
            logger.warning(f"Not running in Kubernetes environment: {e}")
            logger.warning("Running in Docker Compose mode - monitoring metrics only (no scaling)")
            # In Docker Compose mode, just run as a metrics monitor
            run_metrics_monitor()
            return
        
    apps_v1 = client.AppsV1Api()
    session = create_session_with_retries()
    
    while True:
        try:
            # 1. Fetch the latest prediction
            try:
                resp = session.get(ML_SERVICE_URL, timeout=5)
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.RequestException as e:
                logger.error(f"Error accessing ML Service: {e}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            
            if data.get("status") != "ready":
                logger.info(f"ML API returned status '{data.get('status')}', skipping scaling.")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
                
            predicted_rps = data.get("predicted_rps", 0.0)
            
            # Calculate confidence metric from confidence bounds
            # If both bounds exist, use the average as a confidence proxy
            # Otherwise use a default neutral value
            confidence_lower = data.get("confidence_lower")
            confidence_upper = data.get("confidence_upper")
            
            if confidence_lower is not None and confidence_upper is not None:
                confidence_interval_width = confidence_upper - confidence_lower
                # Narrower interval = higher confidence. Normalize to [0,1] scale
                # Assuming typical RPS range 0-200, normalize interval width
                confidence = max(0.0, 1.0 - (confidence_interval_width / (predicted_rps + 1)))
            else:
                confidence = CONFIDENCE_THRESHOLD  # Use threshold as default

            # 2. Calculate Required Pods
            required_pods = int(predicted_rps / TARGET_RPS_PER_POD) if predicted_rps > 0 else MIN_REPLICAS
            required_pods = max(MIN_REPLICAS, min(required_pods, MAX_REPLICAS))

            # 3. Get current replicas
            try:
                deployment = apps_v1.read_namespaced_deployment(DEPLOYMENT_NAME, NAMESPACE)
                current_replicas = deployment.spec.replicas or MIN_REPLICAS
            except client.exceptions.ApiException as e:
                logger.error(f"Kubernetes API Error reading deployment: {e}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            logger.info(f"Predicted RPS: {predicted_rps:.2f} | Required Pods: {required_pods} | Current Replicas: {current_replicas} | Confidence: {confidence:.2f}")

            # 4. Proactive Scale Up (Scale down handled by HPA)
            if required_pods > current_replicas:
                if confidence < CONFIDENCE_THRESHOLD:
                    logger.info(f"Skip scale action because confidence {confidence:.2f} < threshold {CONFIDENCE_THRESHOLD:.2f}")
                else:
                    with last_scale_ts_lock:
                        time_since_last_scale = time.time() - last_scale_ts
                        if time_since_last_scale < SCALE_COOLDOWN_SECONDS:
                            logger.info(f"Skip scale action because cooldown period has not elapsed ({time_since_last_scale:.0f}s < {SCALE_COOLDOWN_SECONDS}s).")
                            time.sleep(POLL_INTERVAL_SECONDS)
                            continue
                    
                    desired_pods = min(current_replicas + MAX_SCALE_DELTA, required_pods)
                    desired_pods = max(MIN_REPLICAS, min(desired_pods, MAX_REPLICAS))

                    if desired_pods > current_replicas:
                        try:
                            logger.info(f"Action: Proactively scaling UP {DEPLOYMENT_NAME} to {desired_pods} replicas.")
                            patch = {"spec": {"replicas": desired_pods}}
                            apps_v1.patch_namespaced_deployment(
                                name=DEPLOYMENT_NAME,
                                namespace=NAMESPACE,
                                body=patch
                            )
                            with last_scale_ts_lock:
                                last_scale_ts = time.time()
                            logger.info("Scale up successful.")
                        except client.exceptions.ApiException as e:
                            logger.error(f"Kubernetes API Error during patch: {e}")
            else:
                logger.debug("No proactive scale up required. HPA remains responsible for drift and scale downs.")
                
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            
        time.sleep(POLL_INTERVAL_SECONDS)

def run_metrics_monitor():
    """Run in Docker Compose mode - just monitor metrics without scaling."""
    logger.info("=" * 60)
    logger.info("DOCKER COMPOSE MODE - Metrics Monitoring Only")
    logger.info("=" * 60)
    logger.info("To enable predictive scaling, deploy to Kubernetes")
    logger.info("")
    
    session = create_session_with_retries()
    
    while True:
        try:
            # Fetch and display metrics
            try:
                resp = session.get(ML_SERVICE_URL, timeout=5)
                resp.raise_for_status()
                data = resp.json()
                
                if data.get("status") == "ready":
                    predicted_rps = data.get("predicted_rps", 0.0)
                    conf_lower = data.get("confidence_lower")
                    conf_upper = data.get("confidence_upper")
                    
                    logger.info(f"📊 Predicted RPS: {predicted_rps:.2f} req/s | "
                              f"Confidence: [{conf_lower:.1f}, {conf_upper:.1f}] | "
                              f"Status: {data.get('status')}")
                else:
                    logger.info(f"⏳ ML Service building model... (status: {data.get('status')})")
            except requests.exceptions.RequestException as e:
                logger.error(f"❌ Error accessing ML Service: {e}")
                
        except Exception as e:
            logger.error(f"Error in metrics monitor: {e}")
            
        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
