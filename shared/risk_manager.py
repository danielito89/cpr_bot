import sys
import os

# Agregamos root al path para importar config
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

class RiskManager:
    def __init__(self, balance):
        self.balance = balance
        self.max_slots = config.RISK_CONFIG['MAX_OPEN_POSITIONS']
        self.risk_s = config.RISK_CONFIG['TIER_S']
        self.risk_a = config.RISK_CONFIG['TIER_A']

    def can_open_position(self, current_open_positions, symbol):
        # 1. Chequeo de Cupos
        if len(current_open_positions) >= self.max_slots:
            return False, "MAX_SLOTS_REACHED"
        
        # 2. Chequeo de Duplicados
        # (Si ya estamos dentro de ese par, no abrimos otro igual)
        for pos in current_open_positions:
            if pos['symbol'] == symbol:
                return False, "ALREADY_IN_POSITION"
                
        return True, "OK"

    def calculate_position_size(self, symbol, entry_price, stop_loss):
        # Calcular distancia al stop
        dist = abs(entry_price - stop_loss)
        if dist == 0: return 0, 0 # Evitar div por cero

        # Determinar Tier del activo desde config
        tier = config.PAIRS_CONFIG.get(symbol, {}).get('tier', 'TIER_A')
        
        # Asignar % de riesgo
        risk_pct = self.risk_s if tier == 'TIER_S' else self.risk_a
        
        # Monto a arriesgar en USD
        risk_usd = self.balance * risk_pct
        
        # Cantidad de monedas
        qty = risk_usd / dist
        
        # Valor nocional (Total de la posición)
        notional_value = qty * entry_price
        
        # CAP DE SEGURIDAD: Nunca poner más del 40% del balance en una sola jugada (aunque el SL esté cerca)
        if notional_value > (self.balance * 0.4):
            qty = (self.balance * 0.4) / entry_price
        
        return qty, notional_value