# core/risk_manager.py
import math

class RiskManager:
    def __init__(self, balance, risk_per_trade=0.03, leverage=10):
        self.balance = balance
        self.risk_per_trade = risk_per_trade
        self.leverage = leverage

    def calculate_position_size(self, entry_price, stop_loss_price):
        """
        Calcula la cantidad de BTC a comprar basándose en el riesgo en USD.
        """
        if entry_price == 0: return 0
        
        # 1. Distancia al Stop Loss en %
        sl_distance_percent = abs(entry_price - stop_loss_price) / entry_price
        
        # 2. Dinero a arriesgar (Ej: $1000 * 0.03 = $30 USD)
        risk_amount = self.balance * self.risk_per_trade
        
        # 3. Tamaño de posición en USD (Ej: $30 / 0.008 = $3750)
        position_size_usd = risk_amount / sl_distance_percent
        
        # 4. Chequeo de Leverage Máximo
        max_position_usd = self.balance * self.leverage
        if position_size_usd > max_position_usd:
            position_size_usd = max_position_usd
            print(f"⚠️ Posición limitada por leverage máx ({self.leverage}x)")

        # 5. Convertir a BTC
        amount_btc = position_size_usd / entry_price
        
        # Redondear a la precisión de Binance (usualmente 3 decimales para BTC)
        return round(amount_btc, 3)