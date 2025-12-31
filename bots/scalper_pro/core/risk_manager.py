import math
import sys
import os

# Ajuste de ruta para encontrar config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class RiskManager:
    def __init__(self, initial_balance=None):
        self.balance = initial_balance if initial_balance else 0.0
        self.leverage = config.LEVERAGE

    def calculate_position_size(self, entry_price, stop_loss_price, quality='STANDARD'):
        """
        Calcula el tamaño de la posición basado en el riesgo % y la distancia al SL.
        Acepta 'quality' para diferenciar entre activos PREMIUM (BTC) y STANDARD (SOL).
        """
        if self.balance <= 0:
            return 0.0

        # 1. Determinar % de Riesgo según Configuración
        if quality == 'PREMIUM':
            risk_pct = config.RISK_PREMIUM  # Ej: 0.03 (3%)
        else:
            risk_pct = config.RISK_STANDARD # Ej: 0.015 (1.5%)

        # 2. Calcular monto en dólares a arriesgar (pérdida máxima)
        risk_amount = self.balance * risk_pct

        # 3. Calcular distancia al Stop Loss
        sl_dist = abs(entry_price - stop_loss_price)
        
        if sl_dist == 0:
            return 0.0

        # 4. Calcular Cantidad de Monedas (Size)
        # Fórmula: Riesgo $$$ / Distancia SL = Cantidad de Monedas
        raw_qty = risk_amount / sl_dist

        # 5. Verificación de Apalancamiento Máximo (Sanity Check)
        # No queremos abrir una posición más grande que Balance * Leverage
        max_notional = self.balance * self.leverage
        notional_value = raw_qty * entry_price

        if notional_value > max_notional:
            raw_qty = max_notional / entry_price
            print(f"⚠️ Posición limitada por apalancamiento ({self.leverage}x)")

        # 6. Redondeo básico según precio (para cumplir mínimos de Binance aprox)
        return self._round_qty(raw_qty, entry_price)

    def _round_qty(self, qty, price):
        """Redondea la cantidad según el valor del activo"""
        if price > 1000: # BTC, ETH
            return round(qty, 3) 
        elif price > 10: # SOL, AVAX, BNB
            return round(qty, 2)
        else: # Altcoins baratas
            return round(qty, 0)