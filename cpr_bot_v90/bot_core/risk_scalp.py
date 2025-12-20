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
        
        # Config V300: Scalp Pullback
        self.risk_per_trade = 0.015  # 1.5% Riesgo
        self.max_leverage = 10.0     # Leverage alto para movimientos cortos
        self.min_rr = 1.5

    async def seek_new_trade(self, _):
        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        # Leer indicadores
        ema_50 = getattr(row, 'ema_50', 0)
        ema_200 = getattr(row, 'ema_200', 0)
        rsi = getattr(row, 'rsi', 50)
        
        if not atr or ema_50 == 0: return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (Trend Pullback) ---
            # 1. Tendencia Sana (EMA 50 > EMA 200)
            is_uptrend = ema_50 > ema_200
            
            # 2. Pullback Zone: Precio cerca de EMA 50 (dentro de 0.5% de distancia)
            # O precio por debajo de EMA 50 pero encima de EMA 200
            dist_to_ema50 = (current_price - ema_50) / ema_50
            in_buy_zone = (dist_to_ema50 < 0.002) and (current_price > ema_200)
            
            # 3. Oversold relativo: RSI bajó un poco (descanso)
            rsi_reset = rsi < 55 # No compramos si RSI está explotado en 80
            
            # 4. Trigger: Vela verde que cierra encima de la EMA 50 (Recuperación)
            # OJO: Usamos la vela actual 'row' que acaba de cerrar en el backtest
            reclaimed = row.close > ema_50 and row.open < ema_50
            
            # Combinación A: Toque y rebote
            if is_uptrend and in_buy_zone and rsi_reset and (row.close > row.open):
                entry = current_price
                # SL: Debajo del swing reciente o 2 ATR
                stop_loss = entry - (atr * 2.0)
                # TP: 2.5R (Scalping busca ratio rápido)
                risk = entry - stop_loss
                take_profit = entry + (risk * 2.5)
                
                if risk > 0:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "Scalp Long EMA50")

            # --- SETUP SHORT ---
            is_downtrend = ema_50 < ema_200
            dist_to_ema50_short = (ema_50 - current_price) / ema_50
            in_sell_zone = (dist_to_ema50_short < 0.002) and (current_price < ema_200)
            rsi_reset_short = rsi > 45 
            
            if is_downtrend and in_sell_zone and rsi_reset_short and (row.close < row.open):
                entry = current_price
                stop_loss = entry + (atr * 2.0)
                risk = stop_loss - entry
                take_profit = entry - (risk * 2.5)
                
                if risk > 0:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "Scalp Short EMA50")

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
                    logging.info(f"!!! SIGNAL V300 !!! {label}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Gestión muy rápida para Scalp
        # BE al 1R
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
                else:
                    pnl_r = (entry - mark) / risk_dist
                
                # BE rápido al 1R para asegurar fees
                if pnl_r > 1.0 and not self.state.sl_moved_to_be:
                    await self.orders_manager.move_sl_to_be(qty)
            except: pass