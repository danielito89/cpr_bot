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
        
        # Config V230: Mean Reversion / Trap Trading
        self.min_rr = 1.5           # MR suele tener RR más bajo pero WR más alto
        self.risk_per_trade = 0.01  # 1% Estándar
        self.max_leverage = 7.0

    # No usamos zonas persistentes en esta estrategia, sino eventos inmediatos
    async def seek_new_trade(self, _):
        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        if not atr: return
        
        # Estructura Fractal (Pre-calculada en Backtester V20)
        # Necesitamos saber dónde estaba la liquidez (Highs/Lows previos)
        last_high = row.last_swing_high
        last_low = row.last_swing_low
        
        if pd.isna(last_high) or pd.isna(last_low): return

        # FILTRO DE RÉGIMEN: Evitar operar contra tendencias nucleares
        # Usamos el body size relativo. Si la vela actual es MONSTRUOSA (>4x ATR), no nos ponemos en medio.
        body_size = abs(row.close - row.open)
        if body_size > (atr * 4.0): return 

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP 1: BEAR TRAP (Falsa ruptura bajista -> LONG) ---
            # 1. El precio perforó el último Low (Tomó liquidez)
            liquidity_grab_low = row.low < last_low
            
            # 2. Pero cerró POR ENCIMA del Low (Rechazo/Fallo)
            rejection_close = row.close > last_low
            
            # 3. La vela es alcista (Verde)
            is_green = row.close > row.open
            
            if liquidity_grab_low and rejection_close and is_green:
                # Entrada: Cierre actual
                entry = current_price
                # SL: Debajo de la mecha del engaño
                stop_loss = row.low - (atr * 0.2)
                # TP: El High opuesto (o al menos hasta la mitad del rango)
                take_profit = last_high
                
                # Validación R/R
                risk = entry - stop_loss
                reward = take_profit - entry
                
                # Si el rango es muy grande, capamos el TP para asegurar WR
                if reward > (risk * 4): 
                    take_profit = entry + (risk * 4)
                
                if risk > 0 and (reward / risk) >= self.min_rr:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "Bear Trap (Fakeout Low)")

            # --- SETUP 2: BULL TRAP (Falsa ruptura alcista -> SHORT) ---
            # 1. El precio perforó el último High
            liquidity_grab_high = row.high > last_high
            
            # 2. Pero cerró POR DEBAJO (Fallo)
            rejection_close_high = row.close < last_high
            
            # 3. Vela bajista (Roja)
            is_red = row.close < row.open
            
            if liquidity_grab_high and rejection_close_high and is_red:
                entry = current_price
                stop_loss = row.high + (atr * 0.2)
                take_profit = last_low
                
                risk = stop_loss - entry
                reward = entry - take_profit
                
                if reward > (risk * 4):
                    take_profit = entry - (risk * 4)
                
                if risk > 0 and (reward / risk) >= self.min_rr:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "Bull Trap (Fakeout High)")

            # --- EJECUCIÓN ---
            if best_setup:
                side, entry, sl, tp, label = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Sizing Dinámico V226 simplificado
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
                    logging.info(f"!!! SIGNAL V230 !!! {label} | R/R: {rr_calc:.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    # --- GESTIÓN ACTIVA V230 (SIMPLE) ---
    async def check_position_state(self):
        # En estrategias de trampa, si no funciona rápido, salimos.
        # Time-stop de 6 velas (6 horas). Si no vamos ganando, fuera.
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
                
                # ZOMBIE KILLER AGRESIVO: 6 horas
                if candles_open >= 6 and current_r < 0.3:
                    await self.orders_manager.close_position_manual("Time Exit (Trap Failed)")
                    return

                # BE AL 1.0R (Asegurar rápido en Mean Reversion)
                if current_r > 1.0 and not self.state.sl_moved_to_be:
                    qty = abs(float(pos['positionAmt']))
                    await self.orders_manager.move_sl_to_be(qty)

            except Exception: pass