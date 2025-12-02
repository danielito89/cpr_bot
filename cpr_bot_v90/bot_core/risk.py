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
        """Devuelve el timestamp actual (Real o Simulado)."""
        if hasattr(self.bot, 'get_current_timestamp'):
            return self.bot.get_current_timestamp()
        return time.time()

    async def can_trade(self, side, current_price):
        """JUEZ DE RIESGO: Decide si se permite abrir nueva posición."""
        # 1. Estado
        if self.state.trading_paused: return False, "Pausado"
        if self.state.is_in_position: return False, "Ya en posición"
        
        now = self._get_now()
        
        # 2. Cooldown
        if now < self.state.trade_cooldown_until:
            wait = int(self.state.trade_cooldown_until - now)
            return False, f"Cooldown ({wait}s)"

        # 3. Filtros de Calendario (Smart Schedule)
        dt_now = datetime.utcfromtimestamp(now)
        
        # A. Filtro de Días (No operar Sábados)
        if dt_now.weekday() == 5: 
            return False, "Día Bloqueado (Sábado)"

        # B. Filtro Horario (UTC)
        # Horas Tóxicas: 04, 10, 13 | Horas Zombie: 00, 06
        FORBIDDEN_HOURS = [0, 4, 6, 10, 13]
        if dt_now.hour in FORBIDDEN_HOURS:
            return False, f"Horario Blacklist ({dt_now.hour}:00 UTC)"

        # 4. Balance
        balance = await self.bot._get_account_balance()
        if balance is None: return False, "Error Balance"
        if balance < self.min_balance_buffer: return False, "Saldo Insuficiente"

        # 5. Límite Diario
        start_bal = self.state.daily_start_balance if self.state.daily_start_balance else balance
        realized_pnl = sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        
        if start_bal > 0:
            daily_pnl_pct = (realized_pnl / start_bal) * 100
            limit_pct = -abs(self.config.daily_loss_limit_pct)
            if daily_pnl_pct <= limit_pct:
                return False, f"Límite Diario ({daily_pnl_pct:.2f}%)"

        # 6. Frecuencia
        if len(self.state.daily_trade_stats) >= self.max_daily_trades:
            return False, "Max Trades Diarios"

        return True, "OK"

    async def seek_new_trade(self, kline):
        current_price = float(kline["c"])
        
        # 1. CHECK DE RIESGO
        can_open, reason = await self.can_trade("CHECK", current_price)
        if not can_open:
            if "Posición" not in reason and time.time() % 60 < 2:
                logging.info(f"[{self.config.symbol}] ⛔ Bloqueado por: {reason}")
            return

        # 2. ESTRATEGIA
        if not self.state.daily_pivots:
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] Esperando pivotes...")
            return
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] Esperando indicadores...")
            return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_volume = float(kline["q"])
                median_vol = self.state.cached_median_vol
                if not median_vol: return
                
                # Filtro Volatilidad
                atr = self.state.cached_atr
                if hasattr(self.config, 'min_volatility_atr_pct'):
                    atr_pct = (atr / current_price) * 100
                    if atr_pct < self.config.min_volatility_atr_pct:
                        if time.time() % 300 < 2:
                            logging.info(f"[{self.config.symbol}] 💤 Mercado Lento: ATR {atr_pct:.2f}%")
                        return

                # --- LÓGICA DE VOLUMEN DINÁMICO (CORREGIDO v106) ---
                p = self.state.daily_pivots
                is_ranging_day = p.get("is_ranging_day", True)
                
                base_factor = self.config.volume_factor
                # FIX: Leer estricto de la config, default 1.5
                strict_factor = getattr(self.config, 'strict_volume_factor', 1.5)
                
                if is_ranging_day:
                    req_vol_range = median_vol * base_factor
                    req_vol_breakout = median_vol * strict_factor
                else:
                    req_vol_range = median_vol * strict_factor
                    req_vol_breakout = median_vol * base_factor
                
                vol_ok_breakout = current_volume > req_vol_breakout
                vol_ok_range = current_volume > req_vol_range
                # ---------------------------------------------------

                ema = self.state.cached_ema
                
                # Diagnóstico
                dist_l4 = abs(current_price - p["L4"]) / current_price * 100
                dist_h4 = abs(current_price - p["H4"]) / current_price * 100
                
                if (dist_l4 < 0.3 or dist_h4 < 0.3) and time.time() % 10 < 2:
                     logging.info(f"[{self.config.symbol}] 🔍 Cerca Breakout. Vol:{'✅' if vol_ok_breakout else '❌'} ({current_volume/1000:.0f}k vs {req_vol_breakout/1000:.0f}k)")

                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                is_green = current_price > open_price
                is_red = current_price < open_price

                # 1. Breakouts (Prioridad)
                if current_price > p["H4"]:
                    if vol_ok_breakout and current_price > ema and is_green:
                        side, entry_type = SIDE_BUY, "Breakout Long"
                        sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                
                elif current_price < p["L4"]:
                    if vol_ok_breakout and current_price < ema and is_red:
                        side, entry_type = SIDE_SELL, "Breakout Short"
                        sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                
                # 2. Rango
                if not side:
                    enable_ranging = getattr(self.config, 'enable_ranging', True)
                    if enable_ranging:
                        if current_price <= p["L3"]:
                            if vol_ok_range and is_green:
                                side, entry_type = SIDE_BUY, "Ranging Long"
                                sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                                potential_tps = [p["L1"], p["H1"], p["H3"]]
                                tp_prices = [tp for tp in potential_tps if tp > current_price]
                                while len(tp_prices) < 3:
                                    base = tp_prices[-1] if tp_prices else current_price
                                    tp_prices.append(base + atr)

                        elif current_price >= p["H3"]:
                            if vol_ok_range and is_red:
                                side, entry_type = SIDE_SELL, "Ranging Short"
                                sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                                potential_tps = [p["H1"], p["L1"], p["L3"]]
                                tp_prices = [tp for tp in potential_tps if tp < current_price]
                                while len(tp_prices) < 3:
                                    base = tp_prices[-1] if tp_prices else current_price
                                    tp_prices.append(base - atr)
                
                if side:
                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    if await self._daily_loss_exceeded(balance): return
                    
                    invest = balance * self.config.investment_pct
                    notional = invest * self.config.leverage
                    if notional > self.max_trade_size_usdt: notional = self.max_trade_size_usdt
                    
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    if entry_type.startswith("Breakout"): tp_prices = [tp_prices[0]]
                    else: tp_prices = tp_prices[:3]

                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    logging.info(f"!!! SEÑAL {self.config.symbol} !!! {entry_type}")
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
                        logging.info(f"[{self.config.symbol}] Cierre detectado (Qty={qty}). Limpiando...")
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

                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice"))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit"))
                
                if qty < self.state.last_known_position_qty:
                    await self._handle_partial_tp(qty)
                
                await self._check_trailing_stop(float(pos.get("markPrice")), qty)

                if self.state.current_position_info.get("entry_type", "").startswith("Ranging"):
                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0:
                        now = self._get_now()
                        if (now - entry_time) / 3600 > 12:
                            await self.orders_manager.close_position_manual(reason="Time Stop 12h")

            except Exception as e:
                if "1003" not in str(e): logging.error(f"[{self.config.symbol}] Check Error: {e}", exc_info=True)

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
        msg = f"{icon} <b>{self.config.symbol} {result_text}</b> {icon}\n" \
              f"<b>PnL Total</b>: <code>{total_pnl:+.2f} USDT</code>\n" \
              f"<b>ROI</b>: <code>{roi:+.2f}%</code>\n" \
              f"<i>{wait_msg}</i>"
        
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
        await self.telegram_handler._send_message(f"🎯 <b>{self.config.symbol} TP{count}</b>\nPnL: <code>{partial_pnl:.2f}</code> | Restante: {qty}")
        
        if count == 2 and not self.state.sl_moved_to_be:
            await self.orders_manager.move_sl_to_be(qty)
    
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