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
        
        # INDICADORES
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
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                
                # --- DEFINICIÓN DE RÉGIMEN ---
                cpr_width = p.get("width", 0)
                is_narrow_cpr = cpr_width < 0.25
                has_slope = abs(ema_slope) > (atr * 0.05)
                
                # 1. Breakout Regime
                is_breakout_regime = is_narrow_cpr and has_slope and (adx > 20)
                
                # 2. Range Regime (Sin slope, adx bajo, cpr ancho)
                is_range_regime = (
                    (adx < 22) and # Un poco más de margen (antes 20)
                    (abs(ema_slope) < (atr * 0.04)) and 
                    (cpr_width > 0.30)
                )

                if is_breakout_regime:
                    await self._check_breakout_signals(
                        current_price, open_price, vol_ratio, adx, ema_200, atr, p, rsi, ema_slope, ema_50
                    )
                elif is_range_regime:
                    await self._check_range_signals(
                        current_price, open_price, vol_ratio, rsi, atr, p, kline
                    )

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # ==========================================
    # MOTOR DE BREAKOUTS + RE-ENTRY (V218)
    # ==========================================
    async def _check_breakout_signals(self, current_price, open_price, vol_ratio, adx, ema_200, atr, p, rsi, ema_slope, ema_50):
        is_uptrend = current_price > ema_200
        is_downtrend = current_price < ema_200
        is_green = current_price > open_price
        is_red = current_price < open_price
        
        has_strong_slope = abs(ema_slope) > (atr * 0.08)
        is_perfect_context = (vol_ratio > 3.2) and (adx > 30) and has_strong_slope
        
        side = None
        entry_type = None
        sl = None
        tp_prices = []
        level_id = None
        size_multiplier = 1.0

        # A. MAIN BREAKOUT
        if vol_ratio > 2.0:
            if is_uptrend and current_price > p["H4"] and is_green and rsi < 70:
                level_id = "BREAK_H4"
                if level_id not in self.levels_traded_today:
                    side = SIDE_BUY
                    entry_type = "Perfect Breakout Long" if is_perfect_context else "Std Breakout Long"
                    if is_perfect_context:
                        size_multiplier = 1.5 
                        # FIX V218: TP1 más cercano para asegurar
                        tp_prices = [
                            current_price + (atr*2.0), 
                            current_price + (atr*5.0), 
                            current_price + (atr*10.0)
                        ]
                    else:
                        size_multiplier = 1.0
                        tp_prices = [current_price + (atr*4.0), current_price + (atr*9.0)]
                    sl = current_price - (atr * 1.2)

            elif is_downtrend and current_price < p["L4"] and is_red and rsi > 30:
                level_id = "BREAK_L4"
                if level_id not in self.levels_traded_today:
                    side = SIDE_SELL
                    entry_type = "Perfect Breakout Short" if is_perfect_context else "Std Breakout Short"
                    if is_perfect_context:
                        size_multiplier = 1.5
                        tp_prices = [
                            current_price - (atr*2.0), 
                            current_price - (atr*5.0), 
                            current_price - (atr*10.0)
                        ]
                    else:
                        size_multiplier = 1.0
                        tp_prices = [current_price - (atr*4.0), current_price - (atr*9.0)]
                    sl = current_price + (atr * 1.2)

        # B. SMART RE-ENTRY (V218: Optimizado)
        if not side and adx > 22:
            dist_to_ema50 = abs(current_price - ema_50) / current_price * 100
            in_value_zone = dist_to_ema50 < 0.4
            rsi_neutral = 40 <= rsi <= 60
            
            # FIX V218: Contar re-entries hoy
            re_entries_count = len([x for x in self.levels_traded_today if "RE_ENTRY" in x])
            can_re_entry = re_entries_count < 2 # Permitir hasta 2
            
            # FIX V218: Size Boost en tendencia fuerte
            base_size = 0.45 if adx > 28 else 0.3
            
            if can_re_entry:
                if is_uptrend and in_value_zone and rsi_neutral and is_green:
                    # ID único para permitir múltiples
                    level_id = f"RE_ENTRY_LONG_{int(time.time())}" 
                    side = SIDE_BUY
                    entry_type = "Smart Re-entry Long"
                    size_multiplier = base_size
                    sl = current_price - (atr * 1.2)
                    tp_prices = [current_price + (atr * 2.0), current_price + (atr * 3.0)]

                elif is_downtrend and in_value_zone and rsi_neutral and is_red:
                    level_id = f"RE_ENTRY_SHORT_{int(time.time())}"
                    side = SIDE_SELL
                    entry_type = "Smart Re-entry Short"
                    size_multiplier = base_size
                    sl = current_price + (atr * 1.2)
                    tp_prices = [current_price - (atr * 2.0), current_price - (atr * 3.0)]

        # EXECUTE
        if side and level_id:
            risk = abs(current_price - sl)
            reward = abs(tp_prices[0] - current_price)
            if risk > 0 and (reward / risk) < 1.05: return

            balance = await self.bot._get_account_balance()
            if not balance: return
            invest = balance * self.config.investment_pct * size_multiplier
            notional = invest * self.config.leverage
            qty = float(format_qty(self.config.step_size, notional / current_price))
            if qty <= 0: return
            tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
            
            self.levels_traded_today.add(level_id)
            logging.info(f"!!! BREAKOUT/RE V218 !!! {entry_type} | Size:{size_multiplier}x")
            await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

    # ==========================================
    # MOTOR DE RANGO MEJORADO (V218)
    # ==========================================
    async def _check_range_signals(self, current_price, open_price, vol_ratio, rsi, atr, p, kline):
        # FIX V218: Vol ratio un poco más permisivo para rango (antes 0.9)
        if vol_ratio > 1.1: return 
        
        is_green = current_price > open_price
        is_red = current_price < open_price
        
        side = None
        entry_type = None
        sl = None
        tp_prices = []
        level_id = None
        size_multiplier = 0.35 
        
        h4_h3_dist = p["H4"] - p["H3"]
        l3_l4_dist = p["L3"] - p["L4"]
        
        short_trigger_zone = p["H4"] - (h4_h3_dist * 0.3)
        long_trigger_zone = p["L4"] + (l3_l4_dist * 0.3)
        
        prev_high = float(kline.get('ph', 999999))
        prev_low = float(kline.get('pl', 0))
        
        failed_high = current_price < prev_high
        failed_low = current_price > prev_low
        
        # FIX V218: Contar range trades hoy
        range_trades_count = len([x for x in self.levels_traded_today if "RANGE" in x])
        if range_trades_count >= 2: return # Max 2 trades de rango por día

        # 1. SHORT DE RANGO
        # FIX V218: RSI > 60 (antes 65), failed_high OR is_red
        in_short_zone = short_trigger_zone < current_price < p["H4"]
        if in_short_zone and (rsi > 60) and (failed_high or is_red):
            level_id = f"RANGE_SHORT_{int(time.time())}" 
            side = SIDE_SELL
            entry_type = "Range Reversion Short"
            sl = p["H4"] + (atr * 0.6)
            tp_prices = [p["P"], p["P"] - (atr * 0.3)]

        # 2. LONG DE RANGO
        # FIX V218: RSI < 40 (antes 35), failed_low OR is_green
        in_long_zone = p["L4"] < current_price < long_trigger_zone
        if in_long_zone and (rsi < 40) and (failed_low or is_green):
            level_id = f"RANGE_LONG_{int(time.time())}"
            side = SIDE_BUY
            entry_type = "Range Reversion Long"
            sl = p["L4"] - (atr * 0.6)
            tp_prices = [p["P"], p["P"] + (atr * 0.3)]

        if side and level_id:
            risk = abs(current_price - sl)
            reward = abs(tp_prices[0] - current_price)
            if risk > 0 and (reward / risk) < 1.0: return

            balance = await self.bot._get_account_balance()
            if not balance: return
            invest = balance * self.config.investment_pct * size_multiplier
            notional = invest * self.config.leverage
            qty = float(format_qty(self.config.step_size, notional / current_price))
            if qty <= 0: return
            tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
            
            self.levels_traded_today.add(level_id)
            logging.info(f"!!! RANGE V218 !!! {entry_type} | RSI:{rsi:.1f} Vol:{vol_ratio:.2f}")
            await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

    # --- GESTIÓN DE TRAILING (V218) ---
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
                
                is_range = "Range" in entry_type
                is_perfect = "Perfect" in entry_type
                is_standard = "Std" in entry_type or "Momentum" in entry_type
                is_reentry = "Re-entry" in entry_type
                
                if atr:
                    pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    
                    if is_range:
                        if pnl_dist > (atr * 0.8) and not self.state.sl_moved_to_be:
                            await self.orders_manager.move_sl_to_be(qty)

                    elif is_perfect:
                        if pnl_dist > (atr * 4.5):
                            new_sl = entry + (atr * 0.1) if side == SIDE_BUY else entry - (atr * 0.1)
                            if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl
                        if pnl_dist > (atr * 7.0):
                            new_sl = ema_50
                            if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl

                    elif is_standard:
                        if pnl_dist > (atr * 2.5) and not self.state.sl_moved_to_be:
                            await self.orders_manager.move_sl_to_be(qty)
                        if pnl_dist > (atr * 4.0):
                             new_sl = entry + (atr * 1.5) if side == SIDE_BUY else entry - (atr * 1.5)
                             if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl

                    elif is_reentry:
                        if pnl_dist > (atr * 1.5) and not self.state.sl_moved_to_be:
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