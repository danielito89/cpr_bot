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
        
        # --- CONFIGURACIÓN V225 (PROFESIONAL) ---
        self.risk_per_trade = 0.02  # Arriesgar 2% de la cuenta por trade
        self.max_leverage = 10      # Leverage disponible para lograr ese riesgo

    def _cleanup_zones(self, current_ts, current_price):
        valid_zones = []
        for z in self.state.active_zones:
            age_candles = (current_ts - z['created_at']) / 3600
            if age_candles > self.zone_validity_candles: continue
            if z.get('attempts', 0) > 0: continue
            
            if z['type'] == 'DEMAND' and current_price < (z['bottom'] * 0.98): continue 
            if z['type'] == 'SUPPLY' and current_price > (z['top'] * 1.02): continue 
            
            valid_zones.append(z)
        self.state.active_zones = valid_zones

    def _create_smart_zone(self, row, prev_row, is_uptrend, is_downtrend):
        if prev_row is None: return
        if not row.is_impulse: return

        if is_uptrend and row.close > row.last_swing_high:
            zone = {
                'type': 'DEMAND',
                'top': prev_row.high,
                'bottom': prev_row.low,
                'created_at': self.state.current_timestamp,
                'tested': False,
                'origin_ts': self.state.current_timestamp,
                'attempts': 0
            }
            self.state.active_zones.append(zone)

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
        prev_row = self.state.prev_row 
        current_price = row.close
        atr = self.state.cached_atr
        
        if not atr: return
        
        sh = row.last_swing_high
        sl = row.last_swing_low
        psh = row.prev_swing_high
        psl = row.prev_swing_low
        
        if pd.isna(sh) or pd.isna(psh): return

        is_uptrend = (sh > psh) and (sl > psl)
        is_downtrend = (sh < psh) and (sl < psl)
        
        self._cleanup_zones(self.state.current_timestamp, current_price)
        self._create_smart_zone(row, prev_row, is_uptrend, is_downtrend)
        
        if not is_uptrend and not is_downtrend: return 
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            for z in self.state.active_zones:
                if z['tested']: continue 
                if self.state.current_timestamp == z['origin_ts']: continue

                # --- DEMAND SETUP ---
                if is_uptrend and z['type'] == 'DEMAND':
                    touched = row.low <= z['top']
                    swept = row.low < (z['bottom'] - (atr * 0.1))
                    
                    body = abs(row.close - row.open)
                    has_displacement = body > (atr * 0.6)
                    
                    confirmed = (row.close > row.open) and \
                                (prev_row is not None and row.close > prev_row.high) and \
                                has_displacement
                    
                    if touched and swept and confirmed:
                        stop_loss = row.low - (atr * 0.5)
                        risk = current_price - stop_loss
                        
                        tp_structure = sh
                        tp_min_rr = current_price + (risk * 2.5)
                        
                        dist_to_struct = abs(tp_structure - current_price)
                        if dist_to_struct > (atr * 6.0):
                            take_profit = tp_min_rr
                        else:
                            take_profit = max(tp_structure, tp_min_rr)
                        
                        reward = take_profit - current_price
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_BUY, current_price, stop_loss, take_profit, "SMC Demand V225", z)

                # --- SUPPLY SETUP ---
                elif is_downtrend and z['type'] == 'SUPPLY':
                    touched = row.high >= z['bottom']
                    swept = row.high > (z['top'] + (atr * 0.1))
                    
                    body = abs(row.close - row.open)
                    has_displacement = body > (atr * 0.6)
                    
                    confirmed = (row.close < row.open) and \
                                (prev_row is not None and row.close < prev_row.low) and \
                                has_displacement
                    
                    if touched and swept and confirmed:
                        stop_loss = row.high + (atr * 0.5)
                        risk = stop_loss - current_price
                        
                        tp_structure = sl 
                        tp_min_rr = current_price - (risk * 2.5)
                        
                        dist_to_struct = abs(current_price - tp_structure)
                        if dist_to_struct > (atr * 6.0):
                            take_profit = tp_min_rr
                        else:
                            take_profit = min(tp_structure, tp_min_rr)
                        
                        reward = current_price - take_profit
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_SELL, current_price, stop_loss, take_profit, "SMC Supply V225", z)

            if best_setup:
                side, entry, sl, tp, label, zone_ref = best_setup
                
                # CÁLCULO DE POSICIÓN DINÁMICA V225 (ESTO ES ORO)
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # 1. ¿Cuánto dinero quiero perder si sale mal?
                risk_amount = balance * self.risk_per_trade # Ej: $1000 * 0.02 = $20
                
                # 2. ¿Cuál es la distancia al stop?
                sl_distance = abs(entry - sl)
                
                if sl_distance <= 0: return

                # 3. ¿Cuántas monedas necesito comprar para que esa distancia sea = $20?
                # Qty = Riesgo_USD / Distancia_Precio
                raw_qty = risk_amount / sl_distance
                
                # 4. Validar con Leverage Máximo
                max_notional = balance * self.max_leverage
                current_notional = raw_qty * entry
                
                # Si la posición calculada excede el apalancamiento máximo, la recortamos
                if current_notional > max_notional:
                    raw_qty = max_notional / entry
                    if self.debug_mode: print(f"⚠️ Posición recortada por Max Leverage 10x")

                qty = float(format_qty(self.config.step_size, raw_qty))
                
                if qty > 0:
                    zone_ref['tested'] = True
                    zone_ref['attempts'] += 1
                    
                    tps = [float(format_price(self.config.tick_size, tp))]
                    rr_calc = (abs(tp-entry)/abs(entry-sl))
                    
                    # Log más detallado para ver el apalancamiento real usado
                    used_leverage = (qty * entry) / balance
                    logging.info(f"!!! SIGNAL V225 !!! {label} | R/R: {rr_calc:.2f} | Lev: {used_leverage:.1f}x")
                    
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)