import math
import time
import logging
from locust import HttpUser, task, between, LoadTestShape, events

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class AppUser(HttpUser):
    """Simulated user that makes requests to the application."""
    wait_time = between(0.1, 0.5)

    @task(5)
    def index(self):
        """Request root endpoint (5x more frequent)."""
        try:
            self.client.get("/")
        except Exception as e:
            logger.error(f"Error in index task: {e}")

    @task(1)
    def heavy_load(self):
        """Request heavy endpoint (1x frequency)."""
        try:
            self.client.get("/heavy")
        except Exception as e:
            logger.error(f"Error in heavy_load task: {e}")

@events.request.add_listener
def on_request_success(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    """Log successful requests."""
    logger.debug(f"Request {name} succeeded - Response time: {response_time}ms")

@events.request.add_listener
def on_request_failure(request_type, name, response_time, response_length, response, context, exception, **kwargs):
    """Log failed requests."""
    logger.warning(f"Request {name} failed - Exception: {exception}")

class SpikyLoadShape(LoadTestShape):
    """
    Simulates a load pattern that triggers hybrid autoscaling:
    
    Stage 1 (0-60s): Base load at 10 users - steady state, predictable
    Stage 2 (60-240s): Gradual increase to 50 users - predictable trend
    Stage 3 (240-480s): Sudden spike to 300 users - unpredictable burst
    Stage 4 (480-840s): Cool down back to 10 users - recovery phase
    
    The predictive controller should scale up during Stage 2.
    The reactive HPA should catch Stage 3 if predictive scaling lags.
    HPA should scale down during Stage 4.
    """
    
    stages = [
        {"duration": 60, "users": 10, "spawn_rate": 2},       # Stage 1: Base load (0-60s)
        {"duration": 240, "users": 50, "spawn_rate": 5},      # Stage 2: Gradual Increase (60-240s)
        {"duration": 480, "users": 300, "spawn_rate": 50},    # Stage 3: Burst/Spike (240-480s)
        {"duration": 840, "users": 10, "spawn_rate": 10},     # Stage 4: Cool down (480-840s)
    ]

    def tick(self):
        """Calculate current stage based on cumulative runtime."""
        run_time = self.get_run_time()
        
        for stage in self.stages:
            if run_time < stage["duration"]:
                tick_data = (stage["users"], stage["spawn_rate"])
                logger.info(f"Stage active - Users: {tick_data[0]}, Spawn rate: {tick_data[1]}")
                return tick_data

        logger.info("All load stages completed - stopping test")
        return None  # Stop test when all stages complete
