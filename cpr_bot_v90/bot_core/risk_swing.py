import logging
import pandas as pd
from datetime import datetime
from .utils import format_price, format_qty, SIDE_BUY, SIDE_SELL

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.config = bot_controller 
        
        # Config V500: Trend Swing Protected
        self.risk_per_trade = 0.03   # 3% (Swing permite más riesgo que Scalp)
        self.max_leverage = 3.0      # Leverage bajo/medio
        
        # Crash Protection (El Freno de Mano)
        self.crash_threshold = -0.05 # Si cae 5% en 6 horas, salimos.
        
        # Trailing
        self.trailing_trigger = 2.0  # Activar al 2R
        self.trailing_dist = 1.5     # Seguir a 1.5R

    async def seek_new_trade(self, _):
        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        ema_50 = getattr(row, 'ema_50', 0)
        ema_200 = getattr(row, 'ema_200', 0)
        rsi = getattr(row, 'rsi', 50)
        crash_metric = getattr(row, 'pct_change_6h', 0)
        
        if not atr or ema_50 == 0: return

        # 0. CRASH PROTECTION (Global)
        # Si el mercado se está desplomando (-5% en 6h), prohibido comprar.
        if crash_metric < self.crash_threshold:
            return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (Trend Pullback 1H) ---
            # 1. Tendencia Sana
            is_uptrend = ema_50 > ema_200
            
            # 2. Momentum Sano (No compramos en divergencia bajista extrema)
            is_momentum = rsi > 50
            
            # 3. Zona de Valor (Precio cerca de EMA 50)
            # No perseguimos precios lejos de la media.
            dist_to_ema = (current_price - ema_50) / ema_50
            in_value_zone = abs(dist_to_ema) < 0.015 # 1.5% de la EMA
            
            # 4. Gatillo: Vela Verde
            is_green = row.close > row.open
            
            if is_uptrend and is_momentum and in_value_zone and is_green:
                entry = current_price
                
                # SL: Debajo de la EMA 200 (Estructural fuerte)
                # Si la EMA 200 está muy lejos, usamos 3 ATR
                dist_ema200 = entry - ema_200
                if dist_ema200 > (atr * 3):
                    stop_loss = entry - (atr * 3)
                else:
                    stop_loss = ema_200 - (atr * 0.2)
                
                # TP: Open (Trend Following) -> Trailing se encarga
                # Ponemos un TP técnico lejano (10R)
                risk = entry - stop_loss
                take_profit = entry + (risk * 10)
                
                if risk > 0:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "Trend Swing 1H")

            # --- SETUP SHORT ---
            is_downtrend = ema_50 < ema_200
            is_momentum_short = rsi < 50
            dist_to_ema_short = (ema_50 - current_price) / ema_50
            in_value_zone_short = abs(dist_to_ema_short) < 0.015
            is_red = row.close < row.open
            
            if is_downtrend and is_momentum_short and in_value_zone_short and is_red:
                entry = current_price
                
                dist_ema200 = ema_200 - entry
                if dist_ema200 > (atr * 3):
                    stop_loss = entry + (atr * 3)
                else:
                    stop_loss = ema_200 + (atr * 0.2)
                
                risk = stop_loss - entry
                take_profit = entry - (risk * 10)
                
                if risk > 0:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "Trend Swing 1H")

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
                    logging.info(f"!!! SIGNAL V500 !!! {label}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Gestión Activa: CRASH MONITOR + TRAILING
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                # --- 1. CRASH PROTECTION (PRIORIDAD MÁXIMA) ---
                # Si detectamos caída acelerada mientras estamos LONG, cerrar todo.
                crash_metric = getattr(self.state.current_row, 'pct_change_6h', 0)
                side = self.state.current_position_info.get('side')
                
                if side == SIDE_BUY and crash_metric < self.crash_threshold:
                    logging.warning(f"🚨 CRASH DETECTED (-5% in 6h). EJECTING POSITIONS.")
                    await self.orders_manager.close_position_manual("Crash Protection")
                    return
                
                # --- 2. TRAILING STOP ---
                entry = float(pos['entryPrice'])
                mark = float(pos['markPrice'])
                sl = self.state.current_position_info.get('sl')
                qty = abs(float(pos['positionAmt']))
                
                if not sl: return
                risk_dist = abs(entry - sl)
                
                if side == SIDE_BUY:
                    pnl_r = (mark - entry) / risk_dist
                    if pnl_r > self.trailing_trigger:
                        new_sl = mark - (risk_dist * self.trailing_dist)
                        if new_sl > sl:
                            await self.orders_manager.update_sl(new_sl, qty)
                            self.state.current_position_info['sl'] = new_sl
                else:
                    pnl_r = (entry - mark) / risk_dist
                    if pnl_r > self.trailing_trigger:
                        new_sl = mark + (risk_dist * self.trailing_dist)
                        if new_sl < sl:
                            await self.orders_manager.update_sl(new_sl, qty)
                            self.state.current_position_info['sl'] = new_sl

            except Exception as e:
                logging.error(f"Error V500 Mgmt: {e}")