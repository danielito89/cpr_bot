import logging
import pandas as pd
from .utils import format_price, format_qty, SIDE_BUY, SIDE_SELL

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.config = bot_controller 
        
        self.zone_validity_candles = 72 # 3 días validez
        self.min_rr = 2.0
        self.debug_mode = False 

    def _cleanup_zones(self, current_ts, current_price):
        valid_zones = []
        for z in self.state.active_zones:
            # 1. Tiempo
            age_candles = (current_ts - z['created_at']) / 3600
            if age_candles > self.zone_validity_candles: continue
            
            # 2. Ruptura (Invalidation)
            if z['type'] == 'DEMAND' and current_price < z['bottom']: continue 
            if z['type'] == 'SUPPLY' and current_price > z['top']: continue 
            
            valid_zones.append(z)
        self.state.active_zones = valid_zones

    def _create_smart_zone(self, row, is_uptrend, is_downtrend):
        # FIX 1: Solo crear si es Impulso
        if not row.is_impulse: return

        # FIX 2: BOS CHECK (Break of Structure)
        # Demand solo si rompió el último High
        if is_uptrend and row.close > row.last_swing_high:
            # FIX 3: LA ZONA ES LA VELA BASE (ANTERIOR), NO EL IMPULSO
            # Si no hay datos de prev, saltamos
            if pd.isna(row.prev_high) or pd.isna(row.prev_low): return
            
            zone = {
                'type': 'DEMAND',
                'top': row.prev_high,  # Todo el rango de la vela base
                'bottom': row.prev_low,
                'created_at': self.state.current_timestamp,
                'tested': False,
                'origin_ts': self.state.current_timestamp
            }
            self.state.active_zones.append(zone)
            if self.debug_mode: print(f"  [+] DEMAND (BOS) creada: {zone['top']}-{zone['bottom']}")

        # Supply solo si rompió el último Low
        elif is_downtrend and row.close < row.last_swing_low:
            if pd.isna(row.prev_high) or pd.isna(row.prev_low): return
            
            zone = {
                'type': 'SUPPLY',
                'top': row.prev_high,
                'bottom': row.prev_low,
                'created_at': self.state.current_timestamp,
                'tested': False,
                'origin_ts': self.state.current_timestamp
            }
            self.state.active_zones.append(zone)
            if self.debug_mode: print(f"  [+] SUPPLY (BOS) creada: {zone['top']}-{zone['bottom']}")

    async def seek_new_trade(self, kline):
        row = self.state.current_row
        current_price = row.close
        
        # 1. DEFINICIÓN DE TENDENCIA
        sh = row.last_swing_high
        sl = row.last_swing_low
        psh = row.prev_swing_high
        psl = row.prev_swing_low
        
        if pd.isna(sh) or pd.isna(psh): return

        is_uptrend = (sh > psh) and (sl > psl)
        is_downtrend = (sh < psh) and (sl < psl)
        
        # 2. GESTIÓN DE ZONAS
        self._cleanup_zones(self.state.current_timestamp, current_price)
        self._create_smart_zone(row, is_uptrend, is_downtrend)
        
        if not is_uptrend and not is_downtrend: return 
        
        # 3. BUSCAR ENTRADA CON CONFIRMACIÓN
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            for z in self.state.active_zones:
                if z['tested']: continue 
                
                # Cooldown: No entrar en la misma vela que se creó la zona (obvio)
                if self.state.current_timestamp == z['origin_ts']: continue

                # UPTREND -> DEMAND PULLBACK
                if is_uptrend and z['type'] == 'DEMAND':
                    # A. El precio tocó la zona (Low de vela actual entró)
                    touched = row.low <= z['top']
                    # B. El precio cerró ALCISTA (Confirmación de rechazo)
                    # OJO: row.close > row.open (Vela verde)
                    confirmed = row.close > row.open
                    
                    if touched and confirmed:
                        stop_loss = z['bottom'] * 0.995 # SL debajo de zona
                        take_profit = sh # TP al último alto
                        
                        risk = current_price - stop_loss
                        reward = take_profit - current_price
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_BUY, current_price, stop_loss, take_profit, "SMC Demand Entry")
                            z['tested'] = True 

                # DOWNTREND -> SUPPLY PULLBACK
                elif is_downtrend and z['type'] == 'SUPPLY':
                    # A. Tocó zona
                    touched = row.high >= z['bottom']
                    # B. Confirmación Bajista (Vela Roja)
                    confirmed = row.close < row.open
                    
                    if touched and confirmed:
                        stop_loss = z['top'] * 1.005 
                        take_profit = sl 
                        
                        risk = stop_loss - current_price
                        reward = current_price - take_profit
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_SELL, current_price, stop_loss, take_profit, "SMC Supply Entry")
                            z['tested'] = True

            if best_setup:
                side, entry, sl, tp, label = best_setup
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Size 10% (HTF es más seguro)
                invest = balance * 0.10
                notional = invest * self.config.leverage
                qty = float(format_qty(self.config.step_size, notional / entry))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    logging.info(f"!!! SIGNAL V221 !!! {label} | R/R: {(abs(tp-entry)/abs(entry-sl)):.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)