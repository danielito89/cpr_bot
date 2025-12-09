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

def calculate_adx(highs, lows, closes, period=14):
    """
    Calcula el ADX (Average Directional Index) manualmente.
    Requiere listas de floats: highs, lows, closes.
    Retorna el último valor de ADX (float) o None.
    """
    try:
        if len(closes) < period * 2:
            return None

        # 1. Calcular True Range (TR) y Directional Moves (DM)
        tr_list = []
        plus_dm_list = []
        minus_dm_list = []

        for i in range(1, len(closes)):
            h, l, c_prev = highs[i], lows[i], closes[i-1]
            
            # TR: Máximo de (H-L, |H-Cp|, |L-Cp|)
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            
            up_move = h - highs[i-1]
            down_move = lows[i-1] - l
            
            # +DM
            if up_move > down_move and up_move > 0:
                plus_dm = up_move
            else:
                plus_dm = 0
            
            # -DM
            if down_move > up_move and down_move > 0:
                minus_dm = down_move
            else:
                minus_dm = 0
                
            tr_list.append(tr)
            plus_dm_list.append(plus_dm)
            minus_dm_list.append(minus_dm)

        # 2. Suavizado (Wilder's Smoothing)
        # Primer valor es promedio simple
        tr_smooth = sum(tr_list[:period])
        plus_dm_smooth = sum(plus_dm_list[:period])
        minus_dm_smooth = sum(minus_dm_list[:period])
        
        dx_list = []
        
        # Calcular el resto de la serie
        for i in range(period, len(tr_list)):
            tr_smooth = tr_smooth - (tr_smooth/period) + tr_list[i]
            plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth/period) + plus_dm_list[i]
            minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth/period) + minus_dm_list[i]
            
            # Calcular DI+ y DI-
            di_plus = 100 * (plus_dm_smooth / tr_smooth) if tr_smooth != 0 else 0
            di_minus = 100 * (minus_dm_smooth / tr_smooth) if tr_smooth != 0 else 0
            
            # Calcular DX
            sum_di = di_plus + di_minus
            dx = 100 * abs(di_plus - di_minus) / sum_di if sum_di != 0 else 0
            dx_list.append(dx)

        # 3. Calcular ADX (Suavizado del DX)
        if len(dx_list) < period:
            return None
            
        # Primer ADX es el promedio de los primeros DX
        adx = sum(dx_list[:period]) / period
        
        # Suavizado final de ADX
        for i in range(period, len(dx_list)):
            adx = ((adx * (period - 1)) + dx_list[i]) / period
            
        return adx

    except Exception as e:
        logging.error(f"Error calculando ADX: {e}")
        return None