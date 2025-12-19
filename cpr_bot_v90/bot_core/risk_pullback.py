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
        
        self.zone_validity_candles = 72 
        self.min_rr = 2.0
        self.debug_mode = False 

    def _cleanup_zones(self, current_ts, current_price):
        valid_zones = []
        for z in self.state.active_zones:
            # 1. Tiempo
            age_candles = (current_ts - z['created_at']) / 3600
            if age_candles > self.zone_validity_candles: continue
            
            # 2. Ruptura (Invalidation)
            # En V222 permitimos que el precio perfore (Sweep) pero no que cierre masivamente fuera
            # Si el precio cierra muy lejos de la zona (ej: > 1% ruptura), se borra.
            # Por simplicidad, mantenemos la limpieza básica, pero el trade logic maneja el sweep
            if z['type'] == 'DEMAND' and current_price < (z['bottom'] * 0.98): continue 
            if z['type'] == 'SUPPLY' and current_price > (z['top'] * 1.02): continue 
            
            valid_zones.append(z)
        self.state.active_zones = valid_zones

    def _create_smart_zone(self, row, is_uptrend, is_downtrend):
        if not row.is_impulse: return

        # Demand Zone (BOS Alcista)
        if is_uptrend and row.close > row.last_swing_high:
            if pd.isna(row.prev_high) or pd.isna(row.prev_low): return
            zone = {
                'type': 'DEMAND',
                'top': row.prev_high,
                'bottom': row.prev_low,
                'created_at': self.state.current_timestamp,
                'tested': False,
                'origin_ts': self.state.current_timestamp
            }
            self.state.active_zones.append(zone)

        # Supply Zone (BOS Bajista)
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

    async def seek_new_trade(self, kline):
        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        if not atr: return
        
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
        
        # 3. BUSCAR ENTRADA (SWEEP + CONFIRMATION)
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            for z in self.state.active_zones:
                if z['tested']: continue 
                if self.state.current_timestamp == z['origin_ts']: continue

                # --- DEMAND SETUP (LONG) ---
                if is_uptrend and z['type'] == 'DEMAND':
                    # A. TOQUE (El precio entra en la zona)
                    touched = row.low <= z['top']
                    
                    # B. SWEEP (El precio barre el mínimo de la zona)
                    # FIX 1: Exigimos que limpie stops
                    swept = row.low < z['bottom']
                    
                    # C. CONFIRMACIÓN FUERTE
                    # FIX 2: Cierra verde Y supera el alto anterior (Engulfing/Strength)
                    confirmed = (row.close > row.open) and (row.close > row.prev_high)
                    
                    if touched and swept and confirmed:
                        # FIX 3: SL debajo de la MECHA ACTUAL (el sweep) + Buffer ATR
                        stop_loss = row.low - (atr * 0.5)
                        
                        # FIX 4: TP Inteligente (Max entre Estructura y 2.5R)
                        risk = current_price - stop_loss
                        tp_structure = sh
                        tp_min_rr = current_price + (risk * 2.5)
                        take_profit = max(tp_structure, tp_min_rr)
                        
                        reward = take_profit - current_price
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_BUY, current_price, stop_loss, take_profit, "SMC Demand Sweep")
                            z['tested'] = True 

                # --- SUPPLY SETUP (SHORT) ---
                elif is_downtrend and z['type'] == 'SUPPLY':
                    # A. TOQUE
                    touched = row.high >= z['bottom']
                    
                    # B. SWEEP
                    swept = row.high > z['top']
                    
                    # C. CONFIRMACIÓN FUERTE (Cierra rojo y rompe bajo anterior)
                    confirmed = (row.close < row.open) and (row.close < row.prev_low)
                    
                    if touched and swept and confirmed:
                        # SL encima de la mecha del sweep + Buffer
                        stop_loss = row.high + (atr * 0.5)
                        
                        risk = stop_loss - current_price
                        tp_structure = sl # Ultimo swing low
                        tp_min_rr = current_price - (risk * 2.5)
                        # OJO: Para short, max price no, "más abajo"
                        take_profit = min(tp_structure, tp_min_rr) 
                        
                        reward = current_price - take_profit
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_SELL, current_price, stop_loss, take_profit, "SMC Supply Sweep")
                            z['tested'] = True

            if best_setup:
                side, entry, sl, tp, label = best_setup
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Size 10% (Estamos en 1H con confirmación)
                invest = balance * 0.10
                notional = invest * self.config.leverage
                qty = float(format_qty(self.config.step_size, notional / entry))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    # Logging mejorado
                    rr_calc = (abs(tp-entry)/abs(entry-sl))
                    logging.info(f"!!! SIGNAL V222 !!! {label} | R/R: {rr_calc:.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)