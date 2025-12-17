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
        rsi = getattr(self.state, 'cached_rsi', 50)
        median_vol = self.state.cached_median_vol
        adx = getattr(self.state, 'cached_adx', 0) # FIX #1: Traemos ADX
        
        if not atr or not ema_200 or not ema_50 or not median_vol: return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_vol = float(kline["q"])
                
                # --- CONTEXTO ---
                is_uptrend = current_price > ema_200
                is_downtrend = current_price < ema_200
                
                # FIX #1: Filtro de Fuerza de Tendencia
                is_trending_strong = adx > 22
                
                cpr_width = p.get("width", 0)
                is_narrow_cpr = cpr_width < 0.25 
                
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                has_breakout_vol = vol_ratio > 2.0
                
                # Horario Prime (06-19 UTC) - Solo para Breakouts
                dt = datetime.utcfromtimestamp(current_ts)
                is_prime_time = 6 <= dt.hour <= 19
                
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                level_id = None
                size_multiplier = 1.0 # Default para Breakouts
                
                is_green = current_price > open_price
                is_red = current_price < open_price

                # ==========================================
                # ESTRATEGIA A: BREAKOUTS (Arma Principal)
                # ==========================================
                # CPR Estrecho + Vol > 2.0 + Prime Time + Full Size
                if is_narrow_cpr and is_prime_time:
                    
                    if is_uptrend and current_price > p["H4"] and is_green:
                        if rsi < 70 and has_breakout_vol:
                            level_id = "BREAK_H4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_BUY
                                entry_type = "Main Breakout Long"
                                size_multiplier = 1.0
                                sl = current_price - (atr * 1.2)
                                tp_prices = [current_price + (atr * 4.0)]

                    elif is_downtrend and current_price < p["L4"] and is_red:
                        if rsi > 30 and has_breakout_vol:
                            level_id = "BREAK_L4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_SELL
                                entry_type = "Main Breakout Short"
                                size_multiplier = 1.0
                                sl = current_price + (atr * 1.2)
                                tp_prices = [current_price - (atr * 4.0)]

                # ==========================================
                # ESTRATEGIA B: SMART RE-ENTRY (Arma Secundaria)
                # ==========================================
                # Requisitos de tus Fixes:
                # 1. Tendencia Fuerte (ADX > 22)
                # 2. Zona de Valor (EMA 50)
                # 3. RSI Neutro (40-60)
                # 4. Una vez al día
                # 5. Size 0.3x
                
                if not side and is_trending_strong: # FIX #1 Aplicado
                    
                    # Zona de EMA 50 (± 0.3%)
                    dist_to_ema50 = abs(current_price - ema_50) / current_price * 100
                    in_value_zone = dist_to_ema50 < 0.3
                    
                    rsi_neutral = 40 <= rsi <= 60
                    
                    # Re-Entry Long
                    if is_uptrend and in_value_zone and rsi_neutral and is_green:
                        level_id = "RE_ENTRY_LONG_DAY" # FIX #2: Cooldown Diario
                        if level_id not in self.levels_traded_today:
                            side = SIDE_BUY
                            entry_type = "Smart Re-entry Long"
                            size_multiplier = 0.3 # FIX #3: Riesgo reducido
                            sl = current_price - (atr * 1.0)
                            # FIX #4: Targets conservadores
                            tp_prices = [
                                current_price + (atr * 1.5), 
                                current_price + (atr * 2.5)
                            ]

                    # Re-Entry Short
                    elif is_downtrend and in_value_zone and rsi_neutral and is_red:
                        level_id = "RE_ENTRY_SHORT_DAY" # FIX #2: Cooldown Diario
                        if level_id not in self.levels_traded_today:
                            side = SIDE_SELL
                            entry_type = "Smart Re-entry Short"
                            size_multiplier = 0.3 # FIX #3: Riesgo reducido
                            sl = current_price + (atr * 1.0)
                            # FIX #4: Targets conservadores
                            tp_prices = [
                                current_price - (atr * 1.5), 
                                current_price - (atr * 2.5)
                            ]

                # --- EJECUCIÓN ---
                if side and level_id:
                    # R/R Check (Mínimo 1.2)
                    risk = abs(current_price - sl)
                    reward = abs(tp_prices[0] - current_price)
                    if risk > 0 and (reward / risk) < 1.2: return

                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    
                    # Aplicar Size Multiplier (1.0 o 0.3)
                    invest = balance * self.config.investment_pct * size_multiplier
                    
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    self.levels_traded_today.add(level_id)
                    logging.info(f"!!! SEÑAL V208 !!! {entry_type} | ADX:{adx:.1f} Size:{size_multiplier}x")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # --- GESTIÓN (Mantenemos 1.5 ATR) ---
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
                
                # Trailing 1.5 ATR
                entry = self.state.current_position_info["entry_price"]
                mark = float(pos.get("markPrice"))
                atr = self.state.cached_atr
                if atr and not self.state.sl_moved_to_be:
                    side = self.state.current_position_info["side"]
                    pnl_dist = (mark - entry) if side == SIDE_BUY else (entry - mark)
                    if pnl_dist > (atr * 1.5): 
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