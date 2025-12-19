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
            
            # 2. Intentos (SMC Real: One shot one kill)
            if z.get('attempts', 0) > 0: continue

            # 3. Ruptura profunda (Invalidation)
            if z['type'] == 'DEMAND' and current_price < (z['bottom'] * 0.98): continue 
            if z['type'] == 'SUPPLY' and current_price > (z['top'] * 1.02): continue 
            
            valid_zones.append(z)
        self.state.active_zones = valid_zones

    def _create_smart_zone(self, row, prev_row, is_uptrend, is_downtrend):
        # FIX V223: Usar el objeto prev_row real del state
        if prev_row is None: return
        if not row.is_impulse: return

        # Demand Zone (BOS Alcista)
        if is_uptrend and row.close > row.last_swing_high:
            zone = {
                'type': 'DEMAND',
                'top': prev_row.high,   # La vela base REAL
                'bottom': prev_row.low,
                'created_at': self.state.current_timestamp,
                'tested': False,
                'origin_ts': self.state.current_timestamp,
                'attempts': 0
            }
            self.state.active_zones.append(zone)

        # Supply Zone (BOS Bajista)
        elif is_downtrend and row.close < row.last_swing_low:
            zone = {
                'type': 'SUPPLY',
                'top': prev_row.high,
                'bottom': prev_row.low,
                'created_at': self.state.current_timestamp,
                'tested': False,
                'origin_ts': self.state.current_timestamp,
                'attempts': 0
            }
            self.state.active_zones.append(zone)

    async def seek_new_trade(self, _):
        row = self.state.current_row
        prev_row = self.state.prev_row # V223 FIX
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
        self._create_smart_zone(row, prev_row, is_uptrend, is_downtrend)
        
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
                    touched = row.low <= z['top']
                    
                    # FIX V223: Sweep Real (con margen)
                    swept = row.low < (z['bottom'] - (atr * 0.1))
                    
                    # FIX V223: Confirmación Fuerte
                    confirmed = (row.close > row.open) and \
                                (prev_row is not None and row.close > prev_row.high)
                    
                    if touched and swept and confirmed:
                        stop_loss = row.low - (atr * 0.5)
                        risk = current_price - stop_loss
                        
                        tp_structure = sh
                        tp_min_rr = current_price + (risk * 2.5)
                        take_profit = max(tp_structure, tp_min_rr)
                        
                        reward = take_profit - current_price
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_BUY, current_price, stop_loss, take_profit, "SMC Demand Sweep", z)

                # --- SUPPLY SETUP (SHORT) ---
                elif is_downtrend and z['type'] == 'SUPPLY':
                    touched = row.high >= z['bottom']
                    
                    swept = row.high > (z['top'] + (atr * 0.1))
                    
                    confirmed = (row.close < row.open) and \
                                (prev_row is not None and row.close < prev_row.low)
                    
                    if touched and swept and confirmed:
                        stop_loss = row.high + (atr * 0.5)
                        risk = stop_loss - current_price
                        
                        tp_structure = sl 
                        tp_min_rr = current_price - (risk * 2.5)
                        take_profit = min(tp_structure, tp_min_rr) 
                        
                        reward = current_price - take_profit
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_SELL, current_price, stop_loss, take_profit, "SMC Supply Sweep", z)

            if best_setup:
                side, entry, sl, tp, label, zone_ref = best_setup
                
                # Marcar zona como usada (One shot)
                zone_ref['tested'] = True
                zone_ref['attempts'] += 1

                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # FIX V223: Size 5% (Control de DD)
                invest = balance * 0.05
                notional = invest * self.config.leverage
                qty = float(format_qty(self.config.step_size, notional / entry))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    rr_calc = (abs(tp-entry)/abs(entry-sl))
                    logging.info(f"!!! SIGNAL V223 !!! {label} | R/R: {rr_calc:.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)