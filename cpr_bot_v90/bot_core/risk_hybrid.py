import logging
import pandas as pd
from .utils import format_price, format_qty, SIDE_BUY, SIDE_SELL

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.config = bot_controller 
        
        # Config V600: Hybrid Trend-Trap
        self.risk_per_trade = 0.02   # 2% Riesgo
        self.max_leverage = 5.0
        self.min_rr = 1.5
        
        # Cooldown para no sobreoperar la misma trampa
        self.last_trade_ts = 0
        self.cooldown_candles = 4    # 4 horas de espera

    async def seek_new_trade(self, _):
        # Cooldown
        if (self.state.current_timestamp - self.last_trade_ts) < (self.cooldown_candles * 3600):
            return

        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        # Indicadores V600
        trend_fast = getattr(row, 'ema_trend_fast', 0) # EMA 200 (1H)
        trend_slow = getattr(row, 'ema_trend_slow', 0) # EMA 800 (1H)
        last_low = getattr(row, 'last_swing_low', 0)
        last_high = getattr(row, 'last_swing_high', 0)
        
        if not atr or trend_fast == 0: return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (Bull Trend + Bear Trap) ---
            # 1. FILTRO DE TENDENCIA 4H (Simulado)
            # Solo buscamos Longs si la estructura macro es alcista
            is_macro_uptrend = trend_fast > trend_slow
            
            # 2. LA TRAMPA (Bear Trap / Liquidity Sweep)
            # El precio perforó el último mínimo...
            swept_low = row.low < last_low
            # ...pero cerró por encima (Rechazo)
            reclaimed = row.close > last_low
            # ...y es una vela verde (Fuerza)
            is_green = row.close > row.open
            
            if is_macro_uptrend and swept_low and reclaimed and is_green:
                entry = current_price
                
                # SL: Debajo de la mecha de la trampa
                stop_loss = row.low - (atr * 0.2)
                
                # TP: Estructural (El último alto)
                # Como vamos a favor de tendencia, esperamos que rompa el alto
                risk = entry - stop_loss
                target_structure = last_high
                
                # Si la estructura está muy cerca (< 1.5R), proyectamos expansión
                if (target_structure - entry) < (risk * 1.5):
                    take_profit = entry + (risk * 2.0)
                else:
                    take_profit = target_structure
                
                # Filtro RR
                if risk > 0 and (take_profit - entry) / risk >= 1.2:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "Hybrid Long (Trend+Trap)")

            # --- SETUP SHORT (Bear Trend + Bull Trap) ---
            is_macro_downtrend = trend_fast < trend_slow
            swept_high = row.high > last_high
            reclaimed_high = row.close < last_high
            is_red = row.close < row.open
            
            if is_macro_downtrend and swept_high and reclaimed_high and is_red:
                entry = current_price
                stop_loss = row.high + (atr * 0.2)
                
                risk = stop_loss - entry
                target_structure = last_low
                
                if (entry - target_structure) < (risk * 1.5):
                    take_profit = entry - (risk * 2.0)
                else:
                    take_profit = target_structure
                
                if risk > 0 and (entry - take_profit) / risk >= 1.2:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "Hybrid Short (Trend+Trap)")

            # EJECUCIÓN
            if best_setup:
                side, entry, sl, tp, label = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                risk_amount = balance * self.risk_per_trade
                sl_distance = abs(entry - sl)
                raw_qty = risk_amount / sl_distance
                
                max_notional = balance * self.max_leverage
                if (raw_qty * entry) > max_notional: raw_qty = max_notional / entry
                
                qty = float(format_qty(self.config.step_size, raw_qty))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    self.last_trade_ts = self.state.current_timestamp
                    
                    logging.info(f"!!! SIGNAL V600 !!! {label}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Gestión Activa: Trailing para dejar correr la tendencia
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                entry = float(pos['entryPrice'])
                mark = float(pos['markPrice'])
                sl = self.state.current_position_info.get('sl')
                side = self.state.current_position_info.get('side')
                qty = abs(float(pos['positionAmt']))
                
                if not sl: return
                risk_dist = abs(entry - sl)
                
                if side == SIDE_BUY:
                    pnl_r = (mark - entry) / risk_dist
                    # Si ya vamos ganando 1.5R, movemos SL a BE
                    if pnl_r > 1.5 and not self.state.sl_moved_to_be:
                        await self.orders_manager.move_sl_to_be(qty)
                else:
                    pnl_r = (entry - mark) / risk_dist
                    if pnl_r > 1.5 and not self.state.sl_moved_to_be:
                        await self.orders_manager.move_sl_to_be(qty)

            except: pass