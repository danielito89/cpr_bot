import logging
import time
import csv
import os
from datetime import datetime
from binance.exceptions import BinanceAPIException
    
from .utils import (
    format_price, format_qty, CSV_HEADER,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
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

    def _get_now(self):
        if hasattr(self.bot, 'get_current_timestamp'):
            return self.bot.get_current_timestamp()
        return time.time()

    async def can_trade(self, side, current_price):
        if self.state.trading_paused: return False, "Pausado"
        if self.state.is_in_position: return False, "Ya en posición"
        
        now = self._get_now()
        if now < self.state.trade_cooldown_until:
            wait = int(self.state.trade_cooldown_until - now)
            return False, f"Cooldown ({wait}s)"

        # NOTA: Los bloqueos de horario duro (Hard Block) se han eliminado en v101.
        # Ahora se gestionan dinámicamente en seek_new_trade con lógica "Hardcore".

        balance = await self.bot._get_account_balance()
        if balance is None: return False, "Error Balance"
        if balance < self.min_balance_buffer: return False, "Saldo Insuficiente"

        start_bal = self.state.daily_start_balance if self.state.daily_start_balance else balance
        realized_pnl = sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        
        if start_bal > 0:
            daily_pnl_pct = (realized_pnl / start_bal) * 100
            if daily_pnl_pct <= -abs(self.config.daily_loss_limit_pct):
                return False, f"Límite Diario ({daily_pnl_pct:.2f}%)"

        if len(self.state.daily_trade_stats) >= self.max_daily_trades:
            return False, "Max Trades Diarios"

        return True, "OK"

    async def seek_new_trade(self, kline):
        current_price = float(kline["c"])
        
        can_open, reason = await self.can_trade("CHECK", current_price)
        if not can_open:
            if "Posición" not in reason and time.time() % 60 < 2:
                logging.info(f"[{self.config.symbol}] ⛔ Bloqueado por: {reason}")
            return

        if not self.state.daily_pivots:
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] Esperando pivotes...")
            return
        
        # Validación estricta de todos los indicadores (incluyendo ADX)
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol, self.state.cached_adx]):
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] Esperando indicadores (ATR, EMA, Vol, ADX)...")
            return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_volume = float(kline["q"])
                
                median_vol = self.state.cached_median_vol
                atr = self.state.cached_atr
                ema = self.state.cached_ema
                adx = self.state.cached_adx
                p = self.state.daily_pivots

                # Filtro de Volatilidad Mínima
                if hasattr(self.config, 'min_volatility_atr_pct'):
                    atr_pct = (atr / current_price) * 100
                    if atr_pct < self.config.min_volatility_atr_pct:
                        if time.time() % 300 < 2:
                            logging.info(f"[{self.config.symbol}] 💤 Mercado Lento: ATR {atr_pct:.2f}%")
                        return

                # --- LÓGICA DINÁMICA DE HORARIOS (La "Regla General") ---
                # En lugar de prohibir, exigimos más confirmación.
                now = self._get_now()
                dt = datetime.utcfromtimestamp(now)
                hour = dt.hour
                is_weekend = dt.weekday() >= 5 # Sábado (5) o Domingo (6)
                is_toxic_hour = hour in [0, 4, 6, 10, 13]
                
                # Factores Base desde Config
                base_vol_factor = self.config.volume_factor          
                strict_vol_factor = getattr(self.config, 'strict_volume_factor', 3.0)
                adx_threshold = 30.0 # Umbral estándar para 1m
                
                # Penalización por Horario Malo ("Modo Hardcore")
                if is_weekend or is_toxic_hour:
                    base_vol_factor *= 2.0
                    strict_vol_factor *= 2.0
                    adx_threshold = 40.0 # Solo tendencias extremas
                    if time.time() % 60 < 2:
                         logging.info(f"[{self.config.symbol}] ⚠️ Modo Hardcore (FinDe/Toxic). Exigiendo Vol x{strict_vol_factor:.1f} ADX>{adx_threshold}")

                # Cálculo de requerimientos de volumen
                req_vol_range = median_vol * base_vol_factor
                req_vol_breakout = median_vol * strict_vol_factor
                
                vol_ok_breakout = current_volume > req_vol_breakout
                vol_ok_range = current_volume > req_vol_range
                
                # --- DIAGNÓSTICO ---
                dist_l4 = abs(current_price - p["L4"]) / current_price * 100
                dist_h4 = abs(current_price - p["H4"]) / current_price * 100
                if (dist_l4 < 0.3 or dist_h4 < 0.3) and time.time() % 10 < 2:
                     logging.info(f"[{self.config.symbol}] 🔍 Cerca Nivel. Px:{current_price} ADX:{adx:.1f} VolStrict:{'✅' if vol_ok_breakout else '❌'}")

                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                is_green = current_price > open_price
                is_red = current_price < open_price
                
                # CLASIFICACIÓN DE RÉGIMEN
                is_trending = adx > adx_threshold
                is_ranging = adx <= adx_threshold

                # 1. ESTRATEGIA DE TENDENCIA (BREAKOUT)
                if is_trending:
                    if current_price > p["H4"]:
                        # Breakout Long
                        if vol_ok_breakout and current_price > ema and is_green:
                            side, entry_type = SIDE_BUY, "Breakout Long (Trend)"
                            sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                            tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                    elif current_price < p["L4"]:
                        # Breakout Short
                        if vol_ok_breakout and current_price < ema and is_red:
                            side, entry_type = SIDE_SELL, "Breakout Short (Trend)"
                            sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                            tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                
                # 2. ESTRATEGIA DE RANGO (REVERSIÓN)
                # Solo si el ADX es débil
                if is_ranging and not side:
                    # Distancia a la EMA (Filtro Anti-Crash)
                    dist_ema = abs(ema - current_price) / current_price * 100
                    
                    # Ranging Long (Compra en soporte L3/L4)
                    if p["L4"] < current_price <= p["L3"]:
                        # Filtro vital: No comprar si se alejó más de un 2% de la media (cuchillo cayendo)
                        if vol_ok_range and is_green and dist_ema < 2.0:
                            side, entry_type = SIDE_BUY, "Ranging Long (Chop)"
                            sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                            potential_tps = [p["L1"], p["H1"], p["H3"]]
                            tp_prices = [tp for tp in potential_tps if tp > current_price]
                            while len(tp_prices) < 3:
                                base = tp_prices[-1] if tp_prices else current_price
                                tp_prices.append(base + atr)

                    # Ranging Short (Venta en resistencia H3/H4)
                    elif p["H3"] <= current_price < p["H4"]:
                        if vol_ok_range and is_red and dist_ema < 2.0:
                            side, entry_type = SIDE_SELL, "Ranging Short (Chop)"
                            sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                            potential_tps = [p["H1"], p["L1"], p["L3"]]
                            tp_prices = [tp for tp in potential_tps if tp < current_price]
                            while len(tp_prices) < 3:
                                base = tp_prices[-1] if tp_prices else current_price
                                tp_prices.append(base - atr)
                
                # --- EJECUCIÓN ---
                if side:
                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    if await self._daily_loss_exceeded(balance): return
                    
                    invest = balance * self.config.investment_pct
                    notional = invest * self.config.leverage
                    if notional > self.max_trade_size_usdt: notional = self.max_trade_size_usdt
                    
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    if "Breakout" in entry_type: tp_prices = [tp_prices[0]]
                    else: tp_prices = tp_prices[:3]
                    
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    logging.info(f"!!! SEÑAL {self.config.symbol} !!! {entry_type} (ADX: {adx:.1f})")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"[{self.config.symbol}] Seek Error: {e}", exc_info=True)

    async def _daily_loss_exceeded(self, balance):
        if balance <= 0: return False
        start_bal = self.state.daily_start_balance if self.state.daily_start_balance else balance
        realized_pnl = sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        if start_bal > 0:
            daily_pnl_pct = (realized_pnl / start_bal) * 100
            if daily_pnl_pct <= -abs(self.config.daily_loss_limit_pct):
                if time.time() > self.state.trade_cooldown_until:
                    await self.telegram_handler._send_message(f"❌ <b>{self.config.symbol}</b>: Límite diario alcanzado.")
                    self.state.trade_cooldown_until = self._get_now() + 86400
                return True
        return False

    async def check_position_state(self):
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                qty = abs(float(pos.get("positionAmt", 0)))
                if qty < 0.0001:
                    if self.state.is_in_position:
                        logging.info(f"[{self.config.symbol}] Cierre detectado. Limpiando...")
                        await self._handle_full_close()
                    return 
                if not self.state.is_in_position and qty > 0:
                    self.state.is_in_position = True
                    self.state.current_position_info = {
                        "quantity": qty, "entry_price": float(pos.get("entryPrice")),
                        "side": SIDE_BUY if float(pos.get("positionAmt")) > 0 else SIDE_SELL,
                        "tps_hit_count": 0, "entry_time": self._get_now(), "total_pnl": 0.0,
                        "unrealized_pnl": float(pos.get("unRealizedProfit", 0.0))
                    }
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    return 
                self.state.current_position_info['mark_price'] = float(pos.get("markPrice"))
                self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit"))
                if qty < self.state.last_known_position_qty:
                    await self._handle_partial_tp(qty)
                await self._check_trailing_stop(float(pos.get("markPrice")), qty)
                
                # Time Stop (Solo para Ranging)
                entry_type = self.state.current_position_info.get("entry_type", "")
                if "Ranging" in entry_type:
                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0:
                        now = self._get_now()
                        if (now - entry_time) / 3600 > 12:
                            await self.orders_manager.close_position_manual(reason="Time Stop 12h")
            except Exception as e:
                if "1003" not in str(e): logging.error(f"[{self.config.symbol}] Check Error: {e}", exc_info=True)

    async def _check_trailing_stop(self, current_price, qty):
        info = self.state.current_position_info
        entry = info.get('entry_price')
        side = info.get('side')
        atr = self.state.cached_atr
        if not atr: return
        trigger = atr * self.config.trailing_stop_trigger_atr
        dist = atr * self.config.trailing_stop_distance_atr
        new_sl = None
        if side == SIDE_BUY:
            if current_price > (entry + trigger):
                pot_sl = current_price - dist
                curr_sl = info.get("trailing_sl_price")
                if curr_sl is None: curr_sl = entry
                if pot_sl > curr_sl: new_sl = pot_sl
        elif side == SIDE_SELL:
            if current_price < (entry - trigger):
                pot_sl = current_price + dist
                curr_sl = info.get("trailing_sl_price")
                if curr_sl is None: curr_sl = entry
                if pot_sl < curr_sl: new_sl = pot_sl
        if new_sl:
            logging.info(f"[{self.config.symbol}] Trailing SL -> {new_sl:.2f}")
            await self.orders_manager.update_sl(new_sl, qty)
            self.state.current_position_info["trailing_sl_price"] = new_sl
            self.state.save_state()

    async def _handle_full_close(self):
        logging.info(f"[{self.config.symbol}] Ejecutando limpieza de cierre...")
        try: await self.client.futures_cancel_all_open_orders(symbol=self.config.symbol)
        except Exception: pass
        old_info = self.state.current_position_info.copy()
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.last_known_position_qty = 0.0
        self.state.sl_moved_to_be = False
        self.state.save_state()
        pnl = 0.0
        roi = 0.0
        try:
            last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
            pnl = float(last_trade.get("realizedPnl", 0.0))
            entry = old_info.get("entry_price", 0)
            qty = old_info.get("quantity", 0)
            if entry > 0 and qty > 0:
                 margin = (entry * qty) / self.config.leverage
                 roi = (pnl / margin) * 100
        except Exception: pass
        total_pnl = old_info.get("total_pnl", 0) + pnl
        self.state.daily_trade_stats.append({"pnl": total_pnl, "roi": roi})
        cooldown = 300
        if total_pnl > 0:
            cooldown = 0
            result_text = "TAKE PROFIT / WIN"
            icon = "✅"
        elif total_pnl < 0:
            cooldown = 900
            result_text = "STOP LOSS"
            icon = "🛑"
        else:
            result_text = "BREAK EVEN"
            icon = "🛡️"
        self.state.trade_cooldown_until = self._get_now() + cooldown
        self.state.save_state()
        wait_msg = f"⏳ Espera: {int(cooldown/60)}m" if cooldown > 0 else "🚀 Listo"
        msg = f"{icon} <b>{self.config.symbol} {result_text}</b> {icon}\n💰 <b>PnL:</b> <code>{total_pnl:+.2f}</code> | <b>ROI:</b> <code>{roi:+.2f}%</code>\n<i>{wait_msg}</i>"
        try: await self.telegram_handler._send_message(msg)
        except: pass
        try:
            td = {
                "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                "entry_type": old_info.get("entry_type", "Unknown"),
                "side": old_info.get("side", "Unknown"),
                "pnl": total_pnl, "pnl_percent_roi": roi, "cooldown": cooldown
            }
            self.bot._log_trade_to_csv(td, self.bot.CSV_FILE)
        except: pass

    async def _handle_partial_tp(self, qty):
        count = self.state.current_position_info.get("tps_hit_count", 0) + 1
        self.state.current_position_info["tps_hit_count"] = count
        self.state.last_known_position_qty = qty
        self.state.save_state()
        partial_pnl = 0.0
        try:
            last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
            partial_pnl = float(last_trade.get("realizedPnl", 0.0))
        except Exception: pass
        logging.info(f"[{self.config.symbol}] TP{count} Parcial. PnL: {partial_pnl}")
        await self.telegram_handler._send_message(f"🎯 <b>{self.config.symbol} TP{count}</b>\nPnL: <code>{partial_pnl:.2f}</code>")
        if count == 1 and not self.state.sl_moved_to_be:
            await self.orders_manager.move_sl_to_be(qty)