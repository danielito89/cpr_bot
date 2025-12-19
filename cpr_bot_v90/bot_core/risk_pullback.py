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
        
        # Risk Config
        self.base_risk = 0.0075      
        self.premium_risk = 0.015    
        self.max_leverage = 7.0
        
        # Configuración V228 (Gestión Activa)
        self.max_trade_duration_candles = 12  # Zombie Killer (12h)
        self.be_trigger_r = 1.5               # Mover a BE al 1.5R

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
        # V228: Eliminado EMA200 filter para capturar giros tempranos
        
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
                        
                        # V228: TP1 Fijo (1.5R) y TP2 Estructural
                        tp1 = current_price + (risk * 1.5)
                        tp2 = max(sh, current_price + (risk * 3.0)) # Runner
                        
                        reward_avg = ((tp1 + tp2) / 2) - current_price
                        
                        # Filtro Base RR (sobre el promedio)
                        if risk > 0 and (reward_avg / risk) >= 1.5:
                            best_setup = (SIDE_BUY, current_price, stop_loss, [tp1, tp2], "SMC Demand V228", z)

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
                        
                        tp1 = current_price - (risk * 1.5)
                        tp2 = min(sl, current_price - (risk * 3.0))
                        
                        reward_avg = current_price - ((tp1 + tp2) / 2)
                        
                        if risk > 0 and (reward_avg / risk) >= 1.5:
                            best_setup = (SIDE_SELL, current_price, stop_loss, [tp1, tp2], "SMC Supply V228", z)

            if best_setup:
                side, entry, sl, tps, label, zone_ref = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Calcular calidad para Sizing
                avg_rr = abs(tps[1] - entry) / abs(entry - sl)
                
                if avg_rr >= 3.5:
                    risk_pct = self.premium_risk 
                    type_label = f"{label} (A+)"
                else:
                    risk_pct = self.base_risk 
                    type_label = f"{label} (Std)"
                
                risk_amount = balance * risk_pct
                sl_distance = abs(entry - sl)
                if sl_distance <= 0: return
                
                raw_qty = risk_amount / sl_distance
                
                max_notional = balance * self.max_leverage
                if (raw_qty * entry) > max_notional:
                    raw_qty = max_notional / entry
                    
                qty = float(format_qty(self.config.step_size, raw_qty))
                
                if qty > 0:
                    zone_ref['tested'] = True
                    zone_ref['attempts'] += 1
                    tps_fmt = [float(format_price(self.config.tick_size, p)) for p in tps]
                    
                    logging.info(f"!!! SIGNAL V228 !!! {type_label} | Risk: {risk_pct*100}%")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps_fmt, type_label)

    # --- V228: GESTIÓN ACTIVA (ZOMBIE KILLER & BE) ---
    async def check_position_state(self):
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                # Info necesaria
                entry_time = self.state.current_position_info.get('entry_time', 0)
                current_time = self.state.current_timestamp
                entry_price = float(pos['entryPrice'])
                mark_price = float(pos['markPrice'])
                sl_price = self.state.current_position_info.get('sl')
                side = self.state.current_position_info.get('side')
                qty = abs(float(pos['positionAmt']))
                
                if not sl_price: return

                # 1. ZOMBIE KILLER (Time-based Exit)
                candles_open = (current_time - entry_time) / 3600
                risk_dist = abs(entry_price - sl_price)
                
                # Calcular PnL actual en R (Unrealized R)
                if side == SIDE_BUY:
                    current_r = (mark_price - entry_price) / risk_dist
                else:
                    current_r = (entry_price - mark_price) / risk_dist
                
                # Si pasaron 12 horas y no vamos ganando al menos 0.5R -> CERRAR
                if candles_open >= self.max_trade_duration_candles and current_r < 0.5:
                    logging.info(f"💀 ZOMBIE KILLER: Trade estancado {candles_open:.1f}h. Closing.")
                    await self.orders_manager.close_position_manual("Time Exit (Stagnant)")
                    return

                # 2. BREAKEVEN AGRESIVO (Si TP1 ya pasó o estamos muy cerca)
                # Si el precio supera 1.5R, proteger la posición
                if current_r > self.be_trigger_r and not self.state.sl_moved_to_be:
                    await self.orders_manager.move_sl_to_be(qty)

            except Exception as e:
                logging.error(f"Error en gestión V228: {e}")