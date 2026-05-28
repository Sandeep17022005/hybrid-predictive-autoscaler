import pandas as pd
from statsmodels.tsa.arima.model import ARIMA
import warnings

class ResourcePredictor:
    """
    ARIMA-based time series predictor for forecasting requests per second (RPS).
    
    Uses an AutoRegressive Integrated Moving Average (ARIMA) model to predict
    future RPS based on historical data. Provides both point predictions and
    confidence intervals.
    """
    
    def __init__(self, order=(1, 1, 1)):
        """
        Initialize the ResourcePredictor.
        
        Args:
            order: ARIMA order tuple (p, d, q)
                - p: Number of autoregressive lags
                - d: Degree of differencing
                - q: Number of moving average terms
        """
        self.order = order
        self.model_fit = None

    def train(self, data: list[float]) -> bool:
        """
        Train the ARIMA model on historical time series data (e.g., RPS over time).
        
        Args:
            data: List of historical RPS measurements
            
        Returns:
            True if training succeeded, False if insufficient data
        """
        if len(data) < 10:
            return False  # Not enough data for reliable training
            
        series = pd.Series(data)
        
        try:
            # Suppress only ARIMA-specific warnings, not all warnings
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=DeprecationWarning)
                model = ARIMA(series, order=self.order)
                self.model_fit = model.fit()
            return True
        except Exception:
            return False

    def predict_next(self, steps: int = 1) -> dict:
        """
        Predict the next `steps` values in the time series with confidence bounds.
        
        Args:
            steps: Number of future time steps to predict (1 to PREDICTION_HORIZON_MINUTES)
            
        Returns:
            Dictionary with:
            - predictions: List of point predictions (RPS values)
            - conf_int: List of confidence interval dicts with "lower" and "upper" bounds
        """
        if self.model_fit is None or steps <= 0 or steps > 60:
            return {
                "predictions": [],
                "conf_int": []
            }

        try:
            forecast = self.model_fit.get_forecast(steps=steps)
            predicted_mean = forecast.predicted_mean
            conf_int_df = forecast.conf_int(alpha=0.05)

            # Clip predictions to non-negative (RPS cannot be negative)
            predictions = [max(0.0, float(v)) for v in predicted_mean]
            
            # Build confidence intervals with non-negative clipping
            conf_int = [
                {
                    "lower": max(0.0, float(conf_int_df.iloc[i, 0])),
                    "upper": max(0.0, float(conf_int_df.iloc[i, 1]))
                }
                for i in range(len(conf_int_df))
            ]

            return {
                "predictions": predictions,
                "conf_int": conf_int
            }
        except Exception:
            return {
                "predictions": [],
                "conf_int": []
            }
