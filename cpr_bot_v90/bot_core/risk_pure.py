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
                
                # --- DIAGNÓSTICO DE REGIMEN ---
                is_uptrend = current_price > ema_200
                is_downtrend = current_price < ema_200
                
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                
                # Trend Day Override (Volumen Explosivo + ADX Fuerte)
                is_trend_day_event = (vol_ratio > 3.0) and (adx > 25)
                
                # Filtro Slope (Solo si no es evento explosivo)
                has_slope = abs(ema_slope) > (atr * 0.05)
                is_valid_trend_context = has_slope or is_trend_day_event
                
                cpr_width = p.get("width", 0)
                is_narrow_cpr = cpr_width < 0.25 
                
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
                # A. BREAKOUTS (Estructura Asimétrica)
                # ==========================================
                # Condición: Narrow CPR O Trend Day Event
                can_breakout = (is_narrow_cpr or is_trend_day_event) and is_valid_trend_context
                
                if can_breakout and (vol_ratio > 1.8):
                    
                    # CAMBIO #3: Detectar "Perfect Setup"
                    is_perfect = is_narrow_cpr and (vol_ratio > 2.5) and (adx > 25)
                    
                    if is_uptrend and current_price > p["H4"] and is_green:
                        limit_rsi = 80 if is_trend_day_event else 70
                        if rsi < limit_rsi:
                            level_id = "BREAK_H4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_BUY
                                entry_type = "Perfect Breakout Long" if is_perfect else "Std Breakout Long"
                                
                                # SIZE & TARGETS
                                if is_perfect:
                                    size_multiplier = 1.3 # CAMBIO #3: Upsizing
                                    # CAMBIO #1: Runner Estructural (2R, 5R, 10R)
                                    tp_prices = [
                                        current_price + (atr * 2.0),
                                        current_price + (atr * 5.0),
                                        current_price + (atr * 10.0)
                                    ]
                                else:
                                    size_multiplier = 1.0
                                    tp_prices = [
                                        current_price + (atr * 2.0),
                                        current_price + (atr * 4.0)
                                    ]
                                
                                sl = current_price - (atr * 1.2) # SL Inicial

                    elif is_downtrend and current_price < p["L4"] and is_red:
                        limit_rsi = 20 if is_trend_day_event else 30
                        if rsi > limit_rsi:
                            level_id = "BREAK_L4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_SELL
                                entry_type = "Perfect Breakout Short" if is_perfect else "Std Breakout Short"
                                
                                if is_perfect:
                                    size_multiplier = 1.3
                                    tp_prices = [
                                        current_price - (atr * 2.0),
                                        current_price - (atr * 5.0),
                                        current_price - (atr * 10.0)
                                    ]
                                else:
                                    size_multiplier = 1.0
                                    tp_prices = [
                                        current_price - (atr * 2.0),
                                        current_price - (atr * 4.0)
                                    ]
                                
                                sl = current_price + (atr * 1.2)

                # ==========================================
                # B. SMART RE-ENTRY (Estructura Conservadora)
                # ==========================================
                if not side and is_valid_trend_context and (adx > 20):
                    
                    dist_to_ema50 = abs(current_price - ema_50) / current_price * 100
                    in_value_zone = dist_to_ema50 < 0.4
                    rsi_neutral = 40 <= rsi <= 60
                    
                    if is_uptrend and in_value_zone and rsi_neutral and is_green:
                        level_id = "RE_ENTRY_LONG_DAY"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Smart Re-entry Long"
                            size_multiplier = 0.4 # CAMBIO #3: Low Risk
                            sl = current_price - (atr * 1.2)
                            # Targets realistas para continuación
                            tp_prices = [
                                current_price + (atr * 1.5), 
                                current_price + (atr * 3.0)
                            ]

                    elif is_downtrend and in_value_zone and rsi_neutral and is_red:
                        level_id = "RE_ENTRY_SHORT_DAY"
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Smart Re-entry Short"
                            size_multiplier = 0.4 
                            sl = current_price + (atr * 1.2)
                            tp_prices = [
                                current_price - (atr * 1.5), 
                                current_price - (atr * 3.0)
                            ]

                # --- EJECUCIÓN ---
                if side and level_id:
                    # Check R/R básico
                    risk = abs(current_price - sl)
                    reward = abs(tp_prices[0] - current_price)
                    if risk > 0 and (reward / risk) < 1.2: return

                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    
                    invest = balance * self.config.investment_pct * size_multiplier
                    
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    self.levels_traded_today.add(level_id)
                    logging.info(f"!!! SEÑAL V210 !!! {entry_type} | Size:{size_multiplier}x Targets:{len(tp_prices)}")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # --- CAMBIO #2: GESTIÓN DE TRAILING DIFERENCIADA Y ESTRUCTURAL ---
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
                    # Restaurar estado si se perdió
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
                
                # --- LÓGICA DE TRAILING AVANZADA ---
                info = self.state.current_position_info
                entry = info["entry_price"]
                mark = float(pos.get("markPrice"))
                atr = self.state.cached_atr
                ema_50 = getattr(self.state, 'cached_ema50', entry) # Usar EMA 50 como trailing
                entry_type = info.get("entry_type", "")
                side = info["side"]
                
                is_breakout = "Breakout" in entry_type
                
                # Caso A: BREAKOUT (Dejar correr el Runner)
                if is_breakout:
                    current_pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    
                    # 1. Si ya pasamos TP1 (ej: +2 ATR), movemos SL a Entry + Costos (NO ANTES)
                    if current_pnl_dist > (atr * 2.0):
                        new_sl = entry + (atr * 0.1) if side == SIDE_BUY else entry - (atr * 0.1)
                        if self._is_better_sl(side, new_sl, info.get("sl")):
                            await self.orders_manager.update_sl(new_sl, qty)
                            info["sl"] = new_sl

                    # 2. Si ya pasamos TP2 (ej: +5 ATR), usamos EMA 50 como Trailing Profundo
                    if current_pnl_dist > (atr * 5.0):
                        # Solo actualizamos si la EMA 50 garantiza ganancia
                        new_sl = ema_50
                        if self._is_better_sl(side, new_sl, info.get("sl")):
                            await self.orders_manager.update_sl(new_sl, qty)
                            info["sl"] = new_sl

                # Caso B: RE-ENTRY (Asegurar rápido)
                else: 
                    current_pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    # Mover a BE tras 1.2 ATR
                    if current_pnl_dist > (atr * 1.2) and not self.state.sl_moved_to_be:
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