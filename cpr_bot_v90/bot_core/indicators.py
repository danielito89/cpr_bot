import logging
import statistics
from datetime import datetime

def calculate_atr(kl_1h, atr_period=14):
    """Calcula el ATR a partir de una lista de klines de 1h."""
    try:
        if not kl_1h or len(kl_1h) <= atr_period:
            logging.warning(f"No hay suficientes klines para ATR (necesita {atr_period}, obtuvo {len(kl_1h)})")
            return None

        highs = [float(k[2]) for k in kl_1h]
        lows = [float(k[3]) for k in kl_1h]
        closes = [float(k[4]) for k in kl_1h]
        trs = []
        for i in range(1, len(kl_1h)):
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)

        if len(trs) >= atr_period:
            first_atr = sum(trs[: atr_period]) / atr_period
            atr = first_atr
            alpha = 1.0 / atr_period
            for tr in trs[atr_period :]:
                atr = (tr * alpha) + (atr * (1 - alpha))
            return atr
        return None
    except Exception as e:
        logging.error(f"Error al calcular ATR: {e}")
        return None

def calculate_ema(kl_ema, ema_period=20):
    """Calcula la EMA a partir de una lista de klines."""
    try:
        if not kl_ema or len(kl_ema) <= ema_period:
            logging.warning(f"No hay suficientes klines para EMA (necesita {ema_period}, obtuvo {len(kl_ema)})")
            return None

        closes_ema = [float(k[4]) for k in kl_ema]
        if len(closes_ema) >= ema_period:
            alpha = 2.0 / (ema_period + 1)
            ema = closes_ema[0]
            for price in closes_ema[1:]:
                ema = (price * alpha) + (ema * (1 - alpha))
            return ema
        return None
    except Exception as e:
        logging.error(f"Error al calcular EMA: {e}")
        return None

def calculate_median_volume(kl_v):
    """Calcula la Mediana del Volumen USDT (k[7]) de klines de 1m."""
    try:
        if kl_v and len(kl_v) > 1:
            # k[7] es Volumen Quote (USDT)
            volumes = [float(k[7]) for k in kl_v[:-1]] 
            if volumes:
                return statistics.median(volumes) # Usar mediana
        return None
    except Exception as e:
        logging.error(f"Error al calcular Mediana de Volumen: {e}")
        return None
