class RiskManager:
    def __init__(self, exchange_handler):
        self.exchange = exchange_handler
        self.max_daily_drawdown_pct = 0.05  # 5% pérdida máxima diaria
        self.max_positions_global = 3       # Máximo 3 trades abiertos a la vez (Scalper + Breakout)
        self.blacklist = []                 # Monedas bloqueadas temporalmente

    def can_open_position(self, symbol):
        """
        Verifica si se permite abrir una nueva posición.
        1. Revisa cantidad de posiciones abiertas.
        2. (Futuro) Revisar Drawdown diario.
        """
        try:
            # Obtener posiciones abiertas en Binance (Futuros)
            balance = self.exchange.get_balance()
            if not balance: return False
            
            # Filtramos posiciones con tamaño > 0
            positions = [p for p in balance['info']['positions'] if float(p['positionAmt']) != 0]
            
            if len(positions) >= self.max_positions_global:
                print(f"🛡️ RISK: Max positions reached ({len(positions)}/{self.max_positions_global})")
                return False

            if symbol in self.blacklist:
                return False

            return True

        except Exception as e:
            print(f"⚠️ Risk Check Error: {e}")
            # Ante la duda, NO operar (Fail-Safe)
            return False

    def get_position_size(self, symbol, risk_per_trade_usd=50):
        """Calcula tamaño de posición. (Por ahora fijo, luego dinámico por ATR)."""
        # Aquí podrías implementar lógica: Size = (Account * 0.01) / Distancia_Stop
        return risk_per_trade_usd # Placeholder