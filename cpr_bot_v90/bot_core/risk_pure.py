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
        ema_50 = getattr(self.state, 'cached_ema50', 0) # Nueva EMA para re-entry
        rsi = getattr(self.state, 'cached_rsi', 50)
        median_vol = self.state.cached_median_vol
        
        if not atr or not ema_200 or not ema_50 or not median_vol: return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_vol = float(kline["q"])
                
                # --- CONTEXTO ---
                is_uptrend = current_price > ema_200
                is_downtrend = current_price < ema_200
                
                cpr_width = p.get("width", 0)
                
                # Clasificación de CPR (Tu Mejora #2)
                cpr_prime = cpr_width < 0.20
                cpr_semi = 0.20 <= cpr_width < 0.40
                
                vol_ratio = current_vol / median_vol if median_vol > 0 else 0
                has_vol = vol_ratio > 1.5 # Volvimos a 1.5 para dar fluidez
                
                # Horario Prime (07-19 UTC) - Solo para Breakouts
                dt = datetime.utcfromtimestamp(current_ts)
                is_prime_time = 7 <= dt.hour <= 19
                
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                level_id = None
                size_multiplier = 1.0 # Default Full Size
                
                is_green = current_price > open_price
                is_red = current_price < open_price

                # ==========================================
                # 1. LOGICA DE BREAKOUTS (Prime & Semi)
                # ==========================================
                if (cpr_prime or cpr_semi) and is_prime_time:
                    
                    # Definir tamaño según calidad del CPR
                    current_size_mult = 1.0 if cpr_prime else 0.5
                    prefix = "Prime" if cpr_prime else "Semi"
                    
                    # Long Breakout H4
                    if is_uptrend and current_price > p["H4"] and is_green:
                        if rsi < 75 and has_vol:
                            level_id = "BREAK_H4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_BUY
                                entry_type = f"{prefix} Breakout Long"
                                size_multiplier = current_size_mult
                                sl = current_price - (atr * 1.2)
                                # Semi targets más cortos (2.5 ATR), Prime (4.0 ATR)
                                tp_mult = 4.0 if cpr_prime else 2.5
                                tp_prices = [current_price + (atr * tp_mult)]

                    # Short Breakout L4
                    elif is_downtrend and current_price < p["L4"] and is_red:
                        if rsi > 25 and has_vol:
                            level_id = "BREAK_L4"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_SELL
                                entry_type = f"{prefix} Breakout Short"
                                size_multiplier = current_size_mult
                                sl = current_price + (atr * 1.2)
                                tp_mult = 4.0 if cpr_prime else 2.5
                                tp_prices = [current_price - (atr * tp_mult)]

                # ==========================================
                # 2. LOGICA DE RE-ENTRY TENDENCIAL (Tu Mejora #1)
                # ==========================================
                # Si no hay breakout, buscamos continuación estructural.
                # Condición: Tocar EMA 50 + RSI Neutro + Estructura de Pivotes
                
                if not side:
                    # Distancia a EMA 50 (Zona de valor)
                    dist_to_ema50 = abs(current_price - ema_50) / current_price * 100
                    in_value_zone = dist_to_ema50 < 0.3 # 0.3% de cercanía a la media
                    
                    # RSI Neutro/Sano para re-entry (40-60)
                    rsi_neutral = 40 <= rsi <= 60
                    
                    # Long Re-entry (Pullback a EMA 50 en Uptrend)
                    if is_uptrend and in_value_zone and rsi_neutral and is_green:
                        # Validar que estamos comprando "barato" dentro del día (ej: debajo de P)
                        if current_price < p["H3"]: 
                            level_id = f"RE_ENTRY_LONG_{dt.hour}" # Permitir uno por hora máx
                            if level_id not in self.levels_traded_today:
                                side = SIDE_BUY
                                entry_type = "Trend Continuation Long"
                                sl = current_price - (atr * 1.0) # Stop ajustado debajo de la media
                                tp_prices = [p["H4"], p["H5"]] # Targets estructurales

                    # Short Re-entry (Pullback a EMA 50 en Downtrend)
                    elif is_downtrend and in_value_zone and rsi_neutral and is_red:
                        if current_price > p["L3"]:
                            level_id = f"RE_ENTRY_SHORT_{dt.hour}"
                            if level_id not in self.levels_traded_today:
                                side = SIDE_SELL
                                entry_type = "Trend Continuation Short"
                                sl = current_price + (atr * 1.0)
                                tp_prices = [p["L4"], p["L5"]]

                # --- EJECUCIÓN ---
                if side and level_id:
                    # R/R Check
                    risk = abs(current_price - sl)
                    reward = abs(tp_prices[0] - current_price)
                    if risk > 0 and (reward / risk) < 1.2: return # Un poco más permisivo en R/R para re-entry

                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    
                    # APLICAR SIZE MULTIPLIER (Gestión de Riesgo V207)
                    invest = balance * self.config.investment_pct * size_multiplier
                    
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    self.levels_traded_today.add(level_id)
                    logging.info(f"!!! SEÑAL V207 !!! {entry_type} | Size:{size_multiplier}x")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"Seek Error: {e}")

    # --- MÉTODOS DE GESTIÓN (Standard) ---
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
                
                # Trailing 1.5 ATR (Equilibrio)
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