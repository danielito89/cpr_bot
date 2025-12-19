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
        
        # Config V230.2: Trap Trading (Fixed)
        self.min_rr = 1.5           
        self.risk_per_trade = 0.01  # 1% Riesgo Fijo
        self.max_leverage = 5.0     # Bajamos leverage para testear estabilidad

        # --- CONTROLES DE FRECUENCIA (V230.2 FIX) ---
        self.last_trade_ts = 0
        self.cooldown_candles = 6   # 6 Horas de silencio tras trade
        self.last_traded_swing_id = None # Para evitar re-entradas en el mismo nivel

    async def seek_new_trade(self, _):
        # 1. COOLDOWN GLOBAL
        # Si operamos hace poco, ignoramos todo
        if (self.state.current_timestamp - self.last_trade_ts) < (self.cooldown_candles * 3600):
            return

        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        if not atr: return
        
        # Estructura Fractal
        last_high = row.last_swing_high
        last_low = row.last_swing_low
        
        if pd.isna(last_high) or pd.isna(last_low): return

        # Crear un ID único para la estructura actual
        # Si los niveles no han cambiado, el ID es el mismo
        current_swing_id = f"{last_high:.2f}_{last_low:.2f}"

        # 2. EVENT LOCK (V230.2 FIX)
        # Si ya operamos esta estructura específica, no hacemos nada
        if self.last_traded_swing_id == current_swing_id:
            return

        # 3. FILTRO DE CONTEXTO (RANGO)
        # Si el rango es demasiado grande (> 3.5 ATR), es expansión/tendencia fuerte.
        # Mean Reversion funciona mejor en rangos comprimidos o normales.
        range_size = abs(last_high - last_low)
        if range_size > (atr * 3.5):
            return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP 1: BEAR TRAP (Fakeout Low -> LONG) ---
            # A. Grab: Precio rompe el Low
            liquidity_grab_low = row.low < last_low
            
            # B. Rejection: Precio cierra DENTRO del rango (encima del Low)
            rejection_close = row.close > last_low
            
            # C. Color: Vela verde
            is_green = row.close > row.open
            
            # D. Displacement (V230.2 FIX): La mecha/cuerpo muestra fuerza
            # Exigimos que el cierre esté alejado del mínimo
            displacement_ok = (row.close - row.low) > (atr * 0.4)
            
            if liquidity_grab_low and rejection_close and is_green and displacement_ok:
                entry = current_price
                stop_loss = row.low - (atr * 0.2)
                
                # E. TP LÓGICO (V230.2 FIX): Mid-Range
                mid_range = (last_high + last_low) / 2
                take_profit = mid_range
                
                # Cap de seguridad 4R
                risk = entry - stop_loss
                reward = take_profit - entry
                
                if reward > (risk * 4): 
                    take_profit = entry + (risk * 4)
                
                if risk > 0 and (reward / risk) >= self.min_rr:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "Bear Trap (Sniper)")

            # --- SETUP 2: BULL TRAP (Fakeout High -> SHORT) ---
            liquidity_grab_high = row.high > last_high
            rejection_close_high = row.close < last_high
            is_red = row.close < row.open
            
            # Displacement Short
            displacement_ok_short = (row.high - row.close) > (atr * 0.4)
            
            if liquidity_grab_high and rejection_close_high and is_red and displacement_ok_short:
                entry = current_price
                stop_loss = row.high + (atr * 0.2)
                
                mid_range = (last_high + last_low) / 2
                take_profit = mid_range
                
                risk = stop_loss - entry
                reward = entry - take_profit
                
                if reward > (risk * 4):
                    take_profit = entry - (risk * 4)
                
                if risk > 0 and (reward / risk) >= self.min_rr:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "Bull Trap (Sniper)")

            # --- EJECUCIÓN ---
            if best_setup:
                side, entry, sl, tp, label = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Sizing Estándar (1%)
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
                    
                    # ACTUALIZAR CONTROLES
                    self.last_trade_ts = self.state.current_timestamp
                    self.last_traded_swing_id = current_swing_id
                    
                    logging.info(f"!!! SIGNAL V230.2 !!! {label} | R/R: {rr_calc:.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    # --- GESTIÓN ACTIVA V230 (SIMPLE) ---
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
                
                # ZOMBIE KILLER: 6 horas (Mean Reversion debe ser rápida)
                if candles_open >= 6 and current_r < 0.3:
                    await self.orders_manager.close_position_manual("Time Exit (Stagnant)")
                    return

                # BE TEMPRANO (0.8R) - Proteger rápido en MR
                if current_r > 0.8 and not self.state.sl_moved_to_be:
                    qty = abs(float(pos['positionAmt']))
                    await self.orders_manager.move_sl_to_be(qty)

            except Exception: pass