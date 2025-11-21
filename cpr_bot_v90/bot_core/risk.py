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
        # 1. Estado
        if self.state.trading_paused: return False, "Pausado"
        if self.state.is_in_position: return False, "Ya en posición"
        
        now = self._get_now()
        if now < self.state.trade_cooldown_until:
            wait = int(self.state.trade_cooldown_until - now)
            return False, f"Cooldown ({wait}s)"

        # 2. Horario
        FORBIDDEN_HOURS = [0, 4, 6, 10, 13]
        current_hour = datetime.utcfromtimestamp(now).hour
        if current_hour in FORBIDDEN_HOURS:
            return False, f"Horario Blacklist ({current_hour}:00 UTC)"

        # 3. Balance
        balance = await self.bot._get_account_balance()
        if balance is None: return False, "Error Balance"
        if balance < self.min_balance_buffer: return False, "Saldo Insuficiente"

        # 4. Límite Diario
        start_bal = self.state.daily_start_balance if self.state.daily_start_balance else balance
        realized_pnl = sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        
        if start_bal > 0:
            daily_pnl_pct = (realized_pnl / start_bal) * 100
            limit_pct = -abs(self.config.daily_loss_limit_pct)
            if daily_pnl_pct <= limit_pct:
                return False, f"Límite Diario ({daily_pnl_pct:.2f}%)"

        if len(self.state.daily_trade_stats) >= self.max_daily_trades:
            return False, "Max Trades Diarios"

        return True, "OK"

    async def seek_new_trade(self, kline):
        current_price = float(kline["c"])
        
        # --- DIAGNÓSTICO ---
        can_open, reason = await self.can_trade("CHECK", current_price)
        if not can_open:
            if "Posición" not in reason and time.time() % 60 < 2:
                logging.info(f"[{self.config.symbol}] ⛔ Bloqueado por: {reason}")
            return

        if not self.state.daily_pivots:
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] ⏳ Esperando Pivotes...")
            return
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] ⏳ Esperando Indicadores...")
            return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_volume = float(kline["q"])
                
                is_green = current_price > open_price
                is_red = current_price < open_price
                
                median_vol = self.state.cached_median_vol
                if not median_vol: return
                
                atr = self.state.cached_atr
                if hasattr(self.config, 'min_volatility_atr_pct'):
                    atr_pct = (atr / current_price) * 100
                    if atr_pct < self.config.min_volatility_atr_pct:
                        if time.time() % 300 < 2:
                            logging.info(f"[{self.config.symbol}] 💤 Mercado Lento: ATR {atr_pct:.2f}%")
                        return

                req_vol = median_vol * self.config.volume_factor
                vol_ok = current_volume > req_vol
                
                p = self.state.daily_pivots
                ema = self.state.cached_ema
                
                # --- DIAGNÓSTICO CERCA DEL NIVEL ---
                dist_l4 = abs(current_price - p["L4"]) / current_price * 100
                dist_h4 = abs(current_price - p["H4"]) / current_price * 100
                dist_l3 = abs(current_price - p["L3"]) / current_price * 100
                dist_h3 = abs(current_price - p["H3"]) / current_price * 100
                
                if (dist_l4 < 0.3 or dist_h4 < 0.3 or dist_l3 < 0.3 or dist_h3 < 0.3) and time.time() % 10 < 2:
                     logging.info(f"[{self.config.symbol}] 🔍 Cerca Nivel. Px:{current_price} Vol:{'✅' if vol_ok else '❌'} Vela:{'🟢' if is_green else '🔴'}")

                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                # 1. Breakouts
                if current_price > p["H4"]:
                    if vol_ok and current_price > ema and is_green:
                        side, entry_type = SIDE_BUY, "Breakout Long"
                        sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                
                elif current_price < p["L4"]:
                    if vol_ok and current_price < ema and is_red:
                        side, entry_type = SIDE_SELL, "Breakout Short"
                        sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                
                # 2. Rango
                if not side:
                    if current_price <= p["L3"]:
                        if vol_ok and is_green:
                            side, entry_type = SIDE_BUY, "Ranging Long"
                            sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                            tp_prices = [current_price + (atr*0.5), current_price + (atr*1.0), current_price + (atr*2.0)]

                    elif current_price >= p["H3"]:
                        if vol_ok and is_red:
                            side, entry_type = SIDE_SELL, "Ranging Short"
                            sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                            tp_prices = [current_price - (atr*0.5), current_price - (atr*1.0), current_price - (atr*2.0)]
                
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
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    logging.info(f"!!! SEÑAL {self.config.symbol} !!! {entry_type}")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"[{self.config.symbol}] Seek Error: {e}", exc_info=True)

    async def check_position_state(self):
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                qty = abs(float(pos.get("positionAmt", 0)))
                
                # Reconciliación (Si el bot estaba ciego y se despierta con posición)
                if not self.state.is_in_position and qty > 0:
                    logging.info(f"[{self.config.symbol}] Posición detectada; sincronizando.")
                    self.state.is_in_position = True
                    self.state.current_position_info = {
                        "quantity": qty, "entry_price": float(pos.get("entryPrice")),
                        "side": SIDE_BUY if float(pos.get("positionAmt")) > 0 else SIDE_SELL,
                        "tps_hit_count": 0, "entry_time": self._get_now(), "total_pnl": 0.0
                    }
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    return 

                # Actualizar precio actual
                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice"))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit"))
                
                # 1. DETECCIÓN DE CIERRE TOTAL (EL FIX BLINDADO)
                if qty == 0 and self.state.is_in_position:
                    logging.info(f"[{self.config.symbol}] Cierre detectado en Binance. Liberando bot...")
                    
                    # --- PASO CRÍTICO: LIBERAR ESTADO PRIMERO ---
                    # Guardamos datos temporales para el reporte
                    old_info = self.state.current_position_info.copy()
                    
                    # Reseteamos estado INMEDIATAMENTE para permitir nuevas operaciones
                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.last_known_position_qty = 0.0
                    self.state.sl_moved_to_be = False
                    self.state.save_state()
                    
                    # --- PASO SECUNDARIO: REPORTAR (Si falla, no bloquea) ---
                    try:
                        # Llamamos a la función de reporte de forma segura
                        # Le pasamos la info vieja porque ya borramos el estado
                        await self._report_full_close(old_info)
                    except Exception as e:
                        logging.error(f"Error reportando cierre (pero el bot ya está libre): {e}")

                    return 
                
                # TP Parcial
                if qty < self.state.last_known_position_qty:
                    await self._handle_partial_tp(qty)
                
                # Trailing Stop
                await self._check_trailing_stop(float(pos.get("markPrice")), qty)

                # Time Stop
                if (not self.state.sl_moved_to_be and 
                    self.state.current_position_info.get("entry_type", "").startswith("Ranging")):
                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0 and (time.time() - entry_time) / 3600 > 12:
                        logging.warning(f"[{self.config.symbol}] Time Stop 12h.")
                        await self.orders_manager.close_position_manual(reason="Time Stop 12h")

            except Exception as e:
                if "1003" not in str(e): logging.error(f"[{self.config.symbol}] Check Error: {e}", exc_info=True)

    # --- NUEVO: REPORTE DE CIERRE DESACOPLADO ---
    async def _report_full_close(self, old_info):
        """Calcula PnL, manda Telegram y guarda CSV. Si falla, no importa."""
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
        
        # Smart Cooldown
        cooldown = 300
        if total_pnl > 0:
            cooldown = 0 # Ganador -> 0m
            logging.info(f"[{self.config.symbol}] WIN (+{total_pnl:.2f}). Sin cooldown.")
        elif total_pnl < 0:
            cooldown = 900 # Perdedor -> 15m
            logging.info(f"[{self.config.symbol}] LOSS ({total_pnl:.2f}). Cooldown 15m.")
            
        self.state.trade_cooldown_until = self._get_now() + cooldown
        
        # Guardar estado de nuevo solo para actualizar el cooldown
        self.state.save_state()

        icon = "✅" if total_pnl >= 0 else "❌"
        msg = f"{icon} <b>{self.config.symbol} CERRADA</b>\nPnL: {total_pnl:.2f} USDT ({roi:.2f}%)"
        await self.telegram_handler._send_message(msg)
        
        td = {
            "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_type": old_info.get("entry_type", "Unknown"),
            "side": old_info.get("side", "Unknown"),
            "pnl": total_pnl, "pnl_percent_roi": roi, "cooldown": cooldown
        }
        self.bot._log_trade_to_csv(td, self.bot.CSV_FILE)

    # ... (Resto de métodos: _check_trailing_stop, _handle_partial_tp, _daily_loss_exceeded IGUALES) ...
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
                curr_sl = info.get("trailing_sl_price") or entry
                if pot_sl > curr_sl: new_sl = pot_sl
        elif side == SIDE_SELL:
            if current_price < (entry - trigger):
                pot_sl = current_price + dist
                curr_sl = info.get("trailing_sl_price") or entry
                if pot_sl < curr_sl: new_sl = pot_sl
        if new_sl:
            logging.info(f"[{self.config.symbol}] Trailing SL -> {new_sl:.2f}")
            await self.orders_manager.update_sl(new_sl, qty)
            self.state.current_position_info["trailing_sl_price"] = new_sl
            self.state.save_state()

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
        if count == 2 and not self.state.sl_moved_to_be:
            await self.orders_manager.move_sl_to_be(qty)

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