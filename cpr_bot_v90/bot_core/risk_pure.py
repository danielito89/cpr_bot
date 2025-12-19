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
        ema_200 = self.state.cached_ema 
        ema_50 = getattr(self.state, 'cached_ema50', 0)
        ema_slope = getattr(self.state, 'cached_ema_slope', 0)
        rsi = getattr(self.state, 'cached_rsi', 50)
        median_vol = self.state.cached_median_vol
        adx = getattr(self.state, 'cached_adx', 0)
        
        if not atr or not ema_200 or not ema_50 or not median_vol: return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_vol = float(kline["q"])
                
                # --- FILTROS DE RÉGIMEN (LO QUE FUNCIONA) ---
                is_uptrend = current_price > ema_200
                is_downtrend = current_price < ema_200
                is_green = current_price > open_price
                is_red = current_price < open_price
                
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                
                # CPR + Slope + ADX = La fórmula del PF 1.43
                cpr_width = p.get("width", 0)
                is_narrow_cpr = cpr_width < 0.25 
                has_slope = abs(ema_slope) > (atr * 0.05)
                is_valid_trend = has_slope and (adx > 20)
                
                # PERFECT SETUP
                has_strong_slope = abs(ema_slope) > (atr * 0.08)
                is_perfect = (
                    is_narrow_cpr 
                    and (vol_ratio > 3.0) 
                    and (adx > 30) 
                    and has_strong_slope
                )

                side = None
                entry_type = None
                sl = None
                tp_prices = []
                level_id = None
                size_multiplier = 1.0 

                # ==========================================
                # SOLO BREAKOUTS (V219 PURGE)
                # ==========================================
                if is_narrow_cpr and is_valid_trend and (vol_ratio > 2.0):
                    
                    # LONG
                    if is_uptrend and current_price > p["H4"] and is_green and rsi < 70:
                        level_id = "BREAK_H4"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Perfect Breakout Long" if is_perfect else "Std Breakout Long"
                            
                            if is_perfect:
                                size_multiplier = 1.5 
                                # TPs Ambiciosos (Runner)
                                tp_prices = [
                                    current_price + (atr * 2.0),
                                    current_price + (atr * 5.0),
                                    current_price + (atr * 12.0)
                                ]
                            else:
                                size_multiplier = 1.0
                                # TPs Sólidos
                                tp_prices = [
                                    current_price + (atr * 3.0),
                                    current_price + (atr * 7.0)
                                ]
                            sl = current_price - (atr * 1.2)

                    # SHORT
                    elif is_downtrend and current_price < p["L4"] and is_red and rsi > 30:
                        level_id = "BREAK_L4"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Perfect Breakout Short" if is_perfect else "Std Breakout Short"
                            
                            if is_perfect:
                                size_multiplier = 1.5
                                tp_prices = [
                                    current_price - (atr * 2.0),
                                    current_price - (atr * 5.0),
                                    current_price - (atr * 12.0)
                                ]
                            else:
                                size_multiplier = 1.0
                                tp_prices = [
                                    current_price - (atr * 3.0),
                                    current_price - (atr * 7.0)
                                ]
                            sl = current_price + (atr * 1.2)

                # --- EJECUCIÓN ---
                if side and level_id:
                    # FILTRO R/R ESTRICTO (Esto nos salvó en V215)
                    risk = abs(current_price - sl)
                    reward = abs(tp_prices[0] - current_price)
                    # Exigimos mínimo 1.6 para entrar
                    if risk > 0 and (reward / risk) < 1.6: return 

                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    invest = balance * self.config.investment_pct * size_multiplier
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    self.levels_traded_today.add(level_id)
                    logging.info(f"!!! SEÑAL V219 (PURE) !!! {entry_type} | Size:{size_multiplier}x")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # --- GESTIÓN (Paciencia) ---
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
                        "tps_hit_count": 0, "entry_time": self._get_now(), "entry_type": "Unknown"
                    }
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    return

                if qty < self.state.last_known_position_qty: await self._handle_partial_tp(qty)
                
                info = self.state.current_position_info
                entry = info["entry_price"]
                mark = float(pos.get("markPrice"))
                atr = self.state.cached_atr
                ema_50 = getattr(self.state, 'cached_ema50', entry) 
                entry_type = info.get("entry_type", "")
                side = info["side"]
                
                is_perfect = "Perfect" in entry_type
                
                if atr:
                    pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    
                    # GESTIÓN PACIENTE (BTC necesita espacio)
                    if is_perfect:
                        # BE muy tarde (4 ATR)
                        if pnl_dist > (atr * 4.0):
                            new_sl = entry + (atr * 0.1) if side == SIDE_BUY else entry - (atr * 0.1)
                            if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl
                        
                        # Trailing EMA 50 al final
                        if pnl_dist > (atr * 8.0):
                            new_sl = ema_50
                            if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl
                    else:
                        # STANDARD: BE tras 2.5 ATR
                        if pnl_dist > (atr * 2.5) and not self.state.sl_moved_to_be:
                            await self.orders_manager.move_sl_to_be(qty)

            except Exception: pass

    def _is_better_sl(self, side, new_sl, current_sl):
        if current_sl is None: return True
        if side == SIDE_BUY: return new_sl > current_sl
        else: return new_sl < current_sl

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