import logging
import time
from datetime import datetime
    
from .utils import (
    format_price, format_qty, 
    SIDE_BUY, SIDE_SELL
)

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.telegram_handler = bot_controller.telegram_handler
        self.config = bot_controller 

        self.max_trade_size_usdt = getattr(self.config, 'MAX_TRADE_SIZE_USDT', 50000)
        self.max_daily_trades = getattr(self.config, 'MAX_DAILY_TRADES', 50) 
        self.min_balance_buffer = 10 
        
        self.last_reset_date = None
        self.levels_traded_today = set()

    def _get_now(self):
        if hasattr(self.bot, 'get_current_timestamp'):
            return self.bot.get_current_timestamp()
        return time.time()

    def _reset_daily_memory_if_needed(self, current_time):
        dt = datetime.fromtimestamp(current_time)
        current_date = dt.date()
        if self.last_reset_date != current_date:
            self.levels_traded_today = set()
            self.last_reset_date = current_date

    async def can_trade(self, side, current_price):
        if self.state.trading_paused: return False, "Pausado"
        if self.state.is_in_position: return False, "Ya en posición"
        
        balance = await self.bot._get_account_balance()
        if not balance or balance < self.min_balance_buffer: return False, "Saldo"
        return True, "OK"

    async def seek_new_trade(self, kline):
        current_ts = self._get_now()
        self._reset_daily_memory_if_needed(current_ts)
        
        current_price = float(kline["c"])
        can_open, _ = await self.can_trade("CHECK", current_price)
        if not can_open: return

        p = self.state.daily_pivots
        if not p: return
        
        atr = self.state.cached_atr
        ema_trend = self.state.cached_ema 
        rsi = getattr(self.state, 'cached_rsi', 50)
        median_vol = self.state.cached_median_vol
        
        if not atr or not ema_trend or not median_vol: return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_vol = float(kline["q"])
                
                # --- CONTEXTO DEL MERCADO ---
                is_uptrend = current_price > ema_trend
                is_downtrend = current_price < ema_trend
                
                cpr_width = p.get("width", 0)
                # MEJORA 1: Permitir rangos más amplios (hasta 0.8% o 1.0%)
                if cpr_width > 0.8: return 

                is_narrow_cpr = cpr_width < 0.20
                
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                level_id = None
                
                is_green = current_price > open_price
                is_red = current_price < open_price

                # ==========================================
                # ESTRATEGIA A: BREAKOUTS (Tendencia Pura)
                # ==========================================
                # Regla: SIEMPRE a favor de la EMA 200.
                if is_narrow_cpr:
                    has_breakout_vol = vol_ratio > 2.0 
                    
                    # Long Breakout H4
                    if is_uptrend and current_price > p["H4"] and is_green:
                        if rsi < 70 and has_breakout_vol:
                            level_id = "BREAK_H4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_BUY
                                entry_type = "Boss Breakout Long"
                                sl = current_price - (atr * 1.0) # SL Ajustado
                                tp_prices = [current_price + (atr * 4.0)]

                    # Short Breakout L4
                    elif is_downtrend and current_price < p["L4"] and is_red:
                        if rsi > 30 and has_breakout_vol:
                            level_id = "BREAK_L4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_SELL
                                entry_type = "Boss Breakout Short"
                                sl = current_price + (atr * 1.0)
                                tp_prices = [current_price - (atr * 4.0)]

                # ==========================================
                # ESTRATEGIA B: REVERSIONES (Mean Reversion)
                # ==========================================
                # Regla: Se permite Contra-Tendencia SI hay extremos de RSI.
                # Ya no exigimos estar arriba/abajo de EMA 200, sino RSI Extremo.
                else:
                    # Reversion Long (Comprar L3)
                    # Ocurre si: Toca L3 Y (Es Uptrend O RSI está sobrevendido < 35)
                    can_long_reversal = (p["L4"] < current_price <= p["L3"]) and is_green
                    if can_long_reversal:
                        # Filtro RSI: Comprar solo si no estamos caros (RSI < 55)
                        # Si es contra-tendencia (Downtrend), exigir RSI < 35 (Sobrevendido)
                        rsi_threshold = 55 if is_uptrend else 35
                        
                        if rsi < rsi_threshold:
                            level_id = "REV_L3"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_BUY
                                entry_type = "Reversal Long (L3)"
                                sl = p["L4"] - (atr * 0.5)
                                tp_prices = [p["H3"]] # Target al otro lado del rango

                    # Reversion Short (Vender H3)
                    can_short_reversal = (p["H3"] <= current_price < p["H4"]) and is_red
                    if can_short_reversal:
                        # Filtro RSI: Vender solo si no estamos baratos (RSI > 45)
                        # Si es contra-tendencia (Uptrend), exigir RSI > 65 (Sobrecompra)
                        rsi_threshold = 45 if is_downtrend else 65
                        
                        if rsi > rsi_threshold:
                            level_id = "REV_H3"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_SELL
                                entry_type = "Reversal Short (H3)"
                                sl = p["H4"] + (atr * 0.5)
                                tp_prices = [p["L3"]]

                # --- EJECUCIÓN ---
                if side and level_id:
                    # R/R Check
                    risk = abs(current_price - sl)
                    reward = abs(tp_prices[0] - current_price)
                    if risk > 0 and (reward / risk) < 1.5: return

                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    
                    invest = balance * self.config.investment_pct
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    self.levels_traded_today.add(level_id)
                    logging.info(f"!!! SEÑAL V204 !!! {entry_type} | RSI:{rsi:.1f}")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # --- GESTIÓN (Ajuste de Trailing) ---
    async def check_position_state(self):
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                qty = abs(float(pos.get("positionAmt", 0)))
                
                if qty < 0.0001:
                    if self.state.is_in_position: await self._handle_full_close()
                    return 
                
                if not self.state.is_in_position and qty > 0:
                    self.state.is_in_position = True
                    self.state.current_position_info = {
                        "quantity": qty, "entry_price": float(pos.get("entryPrice")),
                        "side": SIDE_BUY if float(pos.get("positionAmt")) > 0 else SIDE_SELL,
                        "tps_hit_count": 0, "entry_time": self._get_now()
                    }
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    return

                if qty < self.state.last_known_position_qty: await self._handle_partial_tp(qty)
                
                # MEJORA 3: Trailing a BE más rápido (1.0 ATR)
                entry = self.state.current_position_info["entry_price"]
                mark = float(pos.get("markPrice"))
                atr = self.state.cached_atr
                if atr and not self.state.sl_moved_to_be:
                    side = self.state.current_position_info["side"]
                    pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    
                    # Trigger agresivo: 1.0 ATR para asegurar empate
                    if pnl_dist > (atr * 1.0): 
                        await self.orders_manager.move_sl_to_be(qty)

            except Exception: pass

    async def _handle_full_close(self):
        try: await self.client.futures_cancel_all_open_orders(symbol=self.config.symbol)
        except: pass
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.last_known_position_qty = 0.0
        self.state.sl_moved_to_be = False
        pnl = 0.0
        try:
            last = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
            pnl = float(last.get("realizedPnl", 0.0))
            self.state.daily_trade_stats.append({"pnl": pnl})
        except: pass

    async def _handle_partial_tp(self, qty):
        self.state.last_known_position_qty = qty
        await self.telegram_handler._send_message(f"🎯 TP Parcial")