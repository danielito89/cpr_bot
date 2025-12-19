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
        
        # Config V230.4: Mean Reversion Balanced
        self.min_rr = 1.3           # Subimos vara (Edge real)
        self.risk_per_trade = 0.015 # 1.5% Riesgo (DD es bajo, podemos apretar un poco)
        self.max_leverage = 7.0     

        # Controles
        self.last_trade_ts = 0
        self.cooldown_candles = 6   
        self.last_traded_swing_id = None 
        
        # Gestión Activa V230.4
        self.max_trade_duration_candles = 8   # Más paciencia
        self.be_trigger_r = 1.2               # Dejar respirar

    async def seek_new_trade(self, _):
        # 1. COOLDOWN GLOBAL
        if (self.state.current_timestamp - self.last_trade_ts) < (self.cooldown_candles * 3600):
            return

        row = self.state.current_row
        prev_row = self.state.prev_row 
        
        if prev_row is None: return

        current_price = row.close
        atr = self.state.cached_atr
        
        if not atr: return
        
        # Estructura
        last_high = row.last_swing_high
        last_low = row.last_swing_low
        prev_struct_high = row.prev_swing_high
        prev_struct_low = row.prev_swing_low
        
        if pd.isna(last_high) or pd.isna(last_low): return
        if pd.isna(prev_struct_high) or pd.isna(prev_struct_low): return

        current_swing_id = f"{last_high:.2f}_{last_low:.2f}"

        # 2. EVENT LOCK
        if self.last_traded_swing_id == current_swing_id:
            return

        # 3. FILTROS DE RANGO (V230.4 FIX)
        range_size = abs(last_high - last_low)
        
        # A. Rango demasiado grande (Expansión/Tendencia)
        if range_size > (atr * 4.0): return
        
        # B. Rango demasiado chico (Ruido/Comisiones te comen) - NUEVO
        if range_size < (atr * 1.2): return
            
        # 4. ESTABILIDAD (Pendiente)
        high_shift = abs(last_high - prev_struct_high)
        low_shift = abs(last_low - prev_struct_low)
        
        if high_shift > (atr * 0.8) or low_shift > (atr * 0.8):
            return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP 1: BEAR TRAP (LONG) ---
            liquidity_grab_low = row.low < last_low
            rejection_close = row.close > last_low
            is_green = row.close > row.open
            displacement_ok = (row.close - row.low) > (atr * 0.4)
            prev_was_green = prev_row.close > prev_row.open
            
            if liquidity_grab_low and rejection_close and is_green and displacement_ok and not prev_was_green:
                entry = current_price
                stop_loss = row.low - (atr * 0.2)
                
                # F. TP MID-RANGE (V230.4 FIX)
                mid_range = (last_high + last_low) / 2
                take_profit = mid_range
                
                risk = entry - stop_loss
                reward = take_profit - entry
                
                # Cap de seguridad por si el mid-range es > 4R (raro pero posible)
                if reward > (risk * 4): take_profit = entry + (risk * 4)
                
                if risk > 0 and (reward / risk) >= self.min_rr:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "Bear Trap V230.4")

            # --- SETUP 2: BULL TRAP (SHORT) ---
            liquidity_grab_high = row.high > last_high
            rejection_close_high = row.close < last_high
            is_red = row.close < row.open
            displacement_ok_short = (row.high - row.close) > (atr * 0.4)
            prev_was_red = prev_row.close < prev_row.open
            
            if liquidity_grab_high and rejection_close_high and is_red and displacement_ok_short and not prev_was_red:
                entry = current_price
                stop_loss = row.high + (atr * 0.2)
                
                mid_range = (last_high + last_low) / 2
                take_profit = mid_range
                
                risk = stop_loss - entry
                reward = entry - take_profit
                
                if reward > (risk * 4): take_profit = entry - (risk * 4)
                
                if risk > 0 and (reward / risk) >= self.min_rr:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "Bull Trap V230.4")

            # --- EJECUCIÓN ---
            if best_setup:
                side, entry, sl, tp, label = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Sizing
                risk_amount = balance * self.risk_per_trade
                sl_distance = abs(entry - sl)
                
                raw_qty = risk_amount / sl_distance
                max_notional = balance * self.max_leverage
                
                if (raw_qty * entry) > max_notional:
                    raw_qty = max_notional / entry
                    
                qty = float(format_qty(self.config.step_size, raw_qty))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    rr_calc = (abs(tp-entry)/abs(entry-sl))
                    
                    self.last_trade_ts = self.state.current_timestamp
                    self.last_traded_swing_id = current_swing_id
                    
                    logging.info(f"!!! SIGNAL V230.4 !!! {label} | R/R: {rr_calc:.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    # --- GESTIÓN ACTIVA V230.4 (PACIENTE) ---
    async def check_position_state(self):
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                entry_time = self.state.current_position_info.get('entry_time', 0)
                current_time = self.state.current_timestamp
                entry_price = float(pos['entryPrice'])
                mark_price = float(pos['markPrice'])
                sl_price = self.state.current_position_info.get('sl')
                side = self.state.current_position_info.get('side')
                
                if not sl_price: return

                candles_open = (current_time - entry_time) / 3600
                risk_dist = abs(entry_price - sl_price)
                
                if side == SIDE_BUY:
                    current_r = (mark_price - entry_price) / risk_dist
                else:
                    current_r = (entry_price - mark_price) / risk_dist
                
                # Zombie Killer: 8 horas y < 0.1R (Si no arranca, fuera)
                if candles_open >= self.max_trade_duration_candles and current_r < 0.1:
                    await self.orders_manager.close_position_manual("Time Exit")
                    return

                # BE en 1.2R (Dejamos correr hasta pasar el 1:1)
                if current_r > self.be_trigger_r and not self.state.sl_moved_to_be:
                    qty = abs(float(pos['positionAmt']))
                    await self.orders_manager.move_sl_to_be(qty)

            except Exception: pass