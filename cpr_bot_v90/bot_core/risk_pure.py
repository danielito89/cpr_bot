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
                
                # --- CONTEXTO ---
                is_uptrend = current_price > ema_200
                is_downtrend = current_price < ema_200
                
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                
                # CPR Logic
                cpr_width = p.get("width", 0)
                is_narrow_cpr = cpr_width < 0.25 
                
                # Slope Check
                has_slope = abs(ema_slope) > (atr * 0.05)
                
                # --- DEFINICIÓN DE CONFIGURACIÓN ---
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                level_id = None
                size_multiplier = 1.0 
                
                is_green = current_price > open_price
                is_red = current_price < open_price

                # ==========================================
                # 1. PERFECT BREAKOUT (La Joya de la Corona)
                # ==========================================
                # Narrow CPR + Vol > 2.8 + ADX > 28 + Slope
                is_perfect_context = (
                    is_narrow_cpr and (vol_ratio > 2.8) and (adx > 28) and has_slope
                )
                
                if not side and is_perfect_context:
                    if is_uptrend and current_price > p["H4"] and is_green and rsi < 75:
                        level_id = "BREAK_H4_PERFECT"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Perfect Breakout Long"
                            size_multiplier = 1.5 # CAMBIO B: Agresivo
                            sl = current_price - (atr * 1.2)
                            # CAMBIO A: Fat Tail Targets
                            tp_prices = [
                                current_price + (atr * 2.0),
                                current_price + (atr * 5.0),
                                current_price + (atr * 12.0)
                            ]

                    elif is_downtrend and current_price < p["L4"] and is_red and rsi > 25:
                        level_id = "BREAK_L4_PERFECT"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Perfect Breakout Short"
                            size_multiplier = 1.5
                            sl = current_price + (atr * 1.2)
                            tp_prices = [
                                current_price - (atr * 2.0),
                                current_price - (atr * 5.0),
                                current_price - (atr * 12.0)
                            ]

                # ==========================================
                # 2. STANDARD BREAKOUT (El Pan de cada día)
                # ==========================================
                # Narrow CPR + Vol > 2.0 + ADX > 20
                is_std_context = is_narrow_cpr and (vol_ratio > 2.0) and (adx > 20)
                
                if not side and is_std_context:
                    if is_uptrend and current_price > p["H4"] and is_green and rsi < 70:
                        level_id = "BREAK_H4_STD"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Std Breakout Long"
                            size_multiplier = 1.0
                            sl = current_price - (atr * 1.2)
                            tp_prices = [
                                current_price + (atr * 2.0),
                                current_price + (atr * 4.0)
                            ]
                    
                    elif is_downtrend and current_price < p["L4"] and is_red and rsi > 30:
                        level_id = "BREAK_L4_STD"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Std Breakout Short"
                            size_multiplier = 1.0
                            sl = current_price + (atr * 1.2)
                            tp_prices = [
                                current_price - (atr * 2.0),
                                current_price - (atr * 4.0)
                            ]

                # ==========================================
                # 3. RE-ACELERACIÓN (CAMBIO C: El Nuevo Trigger)
                # ==========================================
                # NO requiere CPR Narrow. Requiere Momentum y Vol > 1.8
                # Rompe H3/L3 con fuerza en tendencia.
                is_reaccel_context = (
                    not is_narrow_cpr # Solo si NO es narrow (para no duplicar)
                    and (vol_ratio > 1.8) 
                    and (adx > 25) 
                    and has_slope
                )
                
                if not side and is_reaccel_context:
                    # Long Re-Accel (Rompe H3 hacia arriba)
                    if is_uptrend and current_price > p["H3"] and is_green and rsi < 70:
                        level_id = "RE_ACCEL_H3"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Re-Accel Breakout Long"
                            size_multiplier = 1.0
                            sl = current_price - (atr * 1.2)
                            tp_prices = [
                                current_price + (atr * 2.0),
                                current_price + (atr * 4.5)
                            ]
                    
                    # Short Re-Accel (Rompe L3 hacia abajo)
                    elif is_downtrend and current_price < p["L3"] and is_red and rsi > 30:
                        level_id = "RE_ACCEL_L3"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Re-Accel Breakout Short"
                            size_multiplier = 1.0
                            sl = current_price + (atr * 1.2)
                            tp_prices = [
                                current_price - (atr * 2.0),
                                current_price - (atr * 4.5)
                            ]

                # ==========================================
                # 4. RE-ENTRY (El Sidearm)
                # ==========================================
                if not side and has_slope and (adx > 22):
                    dist_to_ema50 = abs(current_price - ema_50) / current_price * 100
                    in_value_zone = dist_to_ema50 < 0.4
                    rsi_neutral = 40 <= rsi <= 60
                    
                    if is_uptrend and in_value_zone and rsi_neutral and is_green:
                        level_id = "RE_ENTRY_LONG_DAY"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Smart Re-entry Long"
                            size_multiplier = 0.4
                            sl = current_price - (atr * 1.2)
                            tp_prices = [current_price + (atr * 2.0), current_price + (atr * 3.0)]

                    elif is_downtrend and in_value_zone and rsi_neutral and is_red:
                        level_id = "RE_ENTRY_SHORT_DAY"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Smart Re-entry Short"
                            size_multiplier = 0.4
                            sl = current_price + (atr * 1.2)
                            tp_prices = [current_price - (atr * 2.0), current_price - (atr * 3.0)]

                # --- EJECUCIÓN ---
                if side and level_id:
                    # CAMBIO D: R/R Filter Relajado
                    risk = abs(current_price - sl)
                    reward = abs(tp_prices[0] - current_price)
                    
                    # Solo bloqueamos si es atroz (menos de 1.05)
                    # En Re-entry o Standard permitimos entrar "justo" porque confiamos en la gestión
                    if risk > 0 and (reward / risk) < 1.05: return

                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    
                    invest = balance * self.config.investment_pct * size_multiplier
                    
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    self.levels_traded_today.add(level_id)
                    logging.info(f"!!! SEÑAL V213 !!! {entry_type} | Size:{size_multiplier}x")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # --- CAMBIO A: GESTIÓN DE TRAILING "PULMÓN DE ACERO" ---
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
                
                # --- LÓGICA DE TRAILING ---
                info = self.state.current_position_info
                entry = info["entry_price"]
                mark = float(pos.get("markPrice"))
                atr = self.state.cached_atr
                ema_50 = getattr(self.state, 'cached_ema50', entry) 
                entry_type = info.get("entry_type", "")
                side = info["side"]
                
                is_perfect = "Perfect" in entry_type
                is_standard = "Std" in entry_type
                is_reaccel = "Re-Accel" in entry_type
                is_reentry = "Re-entry" in entry_type
                
                if atr:
                    pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    
                    # 1. PERFECT BREAKOUT (Modo Runner Total)
                    if is_perfect:
                        # BE recién a los 4.5 ATR (Después del TP2)
                        if pnl_dist > (atr * 4.5):
                            new_sl = entry + (atr * 0.1) if side == SIDE_BUY else entry - (atr * 0.1)
                            if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl
                        
                        # Trailing profundo tras 7.0 ATR
                        if pnl_dist > (atr * 7.0):
                            new_sl = ema_50
                            if self._is_better_sl(side, new_sl, info.get("sl")):
                                await self.orders_manager.update_sl(new_sl, qty)
                                info["sl"] = new_sl

                    # 2. STANDARD & RE-ACCEL (Modo Normal)
                    elif is_standard or is_reaccel:
                        # BE tras 2.5 ATR
                        if pnl_dist > (atr * 2.5) and not self.state.sl_moved_to_be:
                            await self.orders_manager.move_sl_to_be(qty)

                    # 3. RE-ENTRY (Modo Rápido)
                    elif is_reentry:
                        # BE tras 2.0 ATR
                        if pnl_dist > (atr * 2.0) and not self.state.sl_moved_to_be:
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