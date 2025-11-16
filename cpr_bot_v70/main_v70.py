#!/usr/bin/env python3
# main_v70.py
# Versión: v70 (Refactorizada con StateManager)

import os
import sys
import time
import json
import shutil
import asyncio
import logging
import signal
import statistics 
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dt_time
from logging.handlers import RotatingFileHandler

from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException

# --- NUEVOS IMPORTS v70 ---
from bot_core.utils import (
    setup_logging, tenacity_retry_decorator_async, 
    format_price, format_qty, CSV_HEADER,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)
from bot_core.pivots import calculate_pivots_from_data
from bot_core.indicators import calculate_atr, calculate_ema, calculate_median_volume
from bot_core.state import StateManager # <-- ¡NUEVO!
from telegram.handler import TelegramHandler
# --- FIN NUEVOS IMPORTS ---

# --- Configuración de Archivos ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_FILE = os.path.join(LOG_DIR, "trading_bot_v70.log")
STATE_FILE = os.path.join(BASE_DIR, "bot_state_v70.json")
CSV_FILE = os.path.join(DATA_DIR, "trades_log_v70.csv")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

logger = setup_logging(LOG_FILE)

# --- Cargar Variables de Entorno ---
API_KEY = os.environ.get("BINANCE_API_KEY") 
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
TESTNET_MODE = os.environ.get("TESTNET_MODE", "false").lower() in ("1", "true", "yes") 
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "5.0"))

if not API_KEY or not API_SECRET:
    logging.critical("Falta BINANCE_API_KEY/BINANCE_SECRET_KEY en las ENV")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logging.warning("Variables de Telegram no configuradas.")

# --- Clase Principal del Bot (Controlador) ---

class BotControllerV70:
    def __init__(
        self,
        symbol="BTCUSDT",
        investment_pct=0.01, 
        leverage=3,         
        cpr_width_threshold=0.2,
        volume_factor=1.3,
        take_profit_levels=3,
        atr_period=14,
        ranging_atr_mult=0.5,
        breakout_atr_sl_mult=1.0,
        breakout_tp_mult=1.25,
        range_tp_mult=2.0,
        ema_period=20,
        ema_timeframe="1h",
    ):
        # --- Configuración de Estrategia ---
        self.symbol = symbol
        self.investment_pct = investment_pct
        self.leverage = leverage
        self.cpr_width_threshold = cpr_width_threshold
        self.volume_factor = volume_factor
        self.take_profit_levels = take_profit_levels
        self.atr_period = atr_period
        self.ranging_atr_multiplier = ranging_atr_mult
        self.breakout_atr_sl_multiplier = breakout_atr_sl_mult
        self.breakout_tp_mult = breakout_tp_mult
        self.range_tp_mult = range_tp_mult
        self.ema_period = ema_period
        self.ema_timeframe = ema_timeframe
        self.daily_loss_limit_pct = DAILY_LOSS_LIMIT_PCT

        # --- Clientes y Handlers ---
        self.client = None
        self.bsm = None

        # --- CAMBIO v70: Crear instancias de los manejadores ---
        self.state = StateManager(STATE_FILE)
        self.telegram_handler = TelegramHandler(
            bot_controller=self, 
            state_manager=self.state, # Pasa el estado
            token=TELEGRAM_BOT_TOKEN, 
            chat_id=TELEGRAM_CHAT_ID
        )

        # --- Reglas de Exchange ---
        self.tick_size = None
        self.step_size = None

        # --- Control ---
        self.lock = asyncio.Lock() # Lock principal para seek_trade y check_position
        self.running = True
        self.account_poll_interval = 5.0
        self.indicator_update_interval_minutes = 15

        # --- FIN __init__ ---


    # --- Lógica de Estado (Ahora delega al StateManager) ---

    def save_state(self):
        self.state.save_state()

    def load_state(self):
        self.state.load_state()

    # --- Comandos de Telegram (Llamados por TelegramHandler) ---

    async def pause_trading(self):
        self.state.trading_paused = True
        self.save_state()
        logging.info("Trading pausado por comando de Telegram.")

    async def resume_trading(self):
        self.state.trading_paused = False
        self.save_state()
        logging.info("Trading reanudado por comando de Telegram.")

    # --- Conexiones de Binance ---

    @tenacity_retry_decorator_async()
    async def _get_klines(self, interval="1h", limit=50):
        return await self.client.futures_klines(symbol=self.symbol, interval=interval, limit=limit)

    @tenacity_retry_decorator_async()
    async def _get_current_position(self):
        positions = await self.client.futures_position_information()
        return next((p for p in positions if p["symbol"] == self.symbol), None)

    @tenacity_retry_decorator_async()
    async def _get_account_balance(self):
        info = await self.client.futures_account()
        for a in info.get("assets", []):
            if a.get("asset") == "USDT":
                return float(a.get("walletBalance", 0.0))
        logging.warning("No USDT asset found in account")
        return None

    @tenacity_retry_decorator_async()
    async def _get_exchange_info(self):
        info = await self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == self.symbol:
                filters = {f["filterType"]: f for f in s["filters"]}
                self.tick_size = filters["PRICE_FILTER"]["tickSize"]
                self.step_size = filters["LOT_SIZE"]["stepSize"]
                logging.info(f"Reglas {self.symbol}: Tick {self.tick_size}, Step {self.step_size}")
                return
        raise Exception("Symbol not found in exchange info")

    # --- Lógica de Indicadores (Orquestador) ---

    async def update_indicators(self):
        try:
            kl_1h = await self._get_klines(interval="1h", limit=50)
            kl_ema = await self._get_klines(interval=self.ema_timeframe, limit=max(self.ema_period * 2, 100))
            kl_1m = await self._get_klines(interval="1m", limit=61)

            # Guardar en el estado
            self.state.cached_atr = calculate_atr(kl_1h, self.atr_period)
            self.state.cached_ema = calculate_ema(kl_ema, self.ema_period)
            self.state.cached_median_vol = calculate_median_volume(kl_1m)

            logging.info(f"Indicadores actualizados: ATR={self.state.cached_atr:.2f}, EMA={self.state.cached_ema:.2f}, VolMed={self.state.cached_median_vol:.0f}")

        except Exception as e:
            logging.error(f"Error actualizando indicadores: {e}")

    # --- Lógica de Pivotes (Orquestador) ---

    async def calculate_pivots(self):
        try:
            kl_1d = await self._get_klines(interval="1d", limit=2)
            if len(kl_1d) < 2:
                raise Exception("Insufficient daily klines for pivots")

            y = kl_1d[-2]
            h, l, c = float(y[2]), float(y[3]), float(y[4])

            # Guardar en el estado
            self.state.daily_pivots = calculate_pivots_from_data(
                h, l, c, self.tick_size, self.cpr_width_threshold
            )

            if self.state.daily_pivots:
                self.state.last_pivots_date = datetime.utcnow().date()
                logging.info("Pivotes (Camarilla Clásica) actualizados")
                await self.telegram_handler._send_message(self.telegram_handler._pivots_text())
            else:
                raise Exception("Cálculo de pivotes devolvió None")

        except Exception as e:
            logging.error(f"Error al calcular pivotes: {e}")
            await self.telegram_handler._send_message("🚨 <b>ERROR</b>\nFallo al calcular pivotes iniciales. Bot inactivo.")

    # --- Lógica de Órdenes (Se moverá a orders.py) ---

    async def _place_bracket_order(self, side, qty, entry_price_signal, sl_price, tp_prices, entry_type):
        try:
            mark_price_entry = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
            logging.info("Enviando MARKET %s %s %s", side, qty, self.symbol)
            market = await self.client.futures_create_order(
                symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET, quantity=format_qty(self.step_size, qty)
            )
        except BinanceAPIException as e:
            logging.error(f"Market order failed: {e}")
            await self.telegram_handler._send_message(f"❌ <b>ERROR ENTRY</b>\n{e}")
            self.state.trade_cooldown_until = time.time() + 300
            return

        filled, attempts, order_id = False, 0, market.get("orderId")
        avg_price, executed_qty = 0.0, 0.0

        while attempts < 15:
            try:
                status = await self.client.futures_get_order(symbol=self.symbol, orderId=order_id)
                if status.get("status") == "FILLED":
                    filled, avg_price, executed_qty = True, float(status.get("avgPrice", 0)), abs(float(status.get("executedQty", 0)))
                    break
            except Exception: pass
            attempts += 1
            await asyncio.sleep(0.5 + attempts * 0.1)

        if not filled:
            logging.error("Market order not confirmed filled; cooldown set")
            await self.telegram_handler._send_message("❌ <b>ERROR CRÍTICO</b>\nMARKET no confirmado FILLED.")
            self.state.trade_cooldown_until = time.time() + 300
            return

        sl_order_id = None
        try:
            batch = []
            num_tps = min(len(tp_prices), self.take_profit_levels)
            if num_tps == 0: raise Exception("No TP prices")
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))

            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            if (side == SIDE_BUY and float(sl_price) >= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])) or \
               (side == SIDE_SELL and float(sl_price) <= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])):
                raise Exception("SL already surpassed by market price (fail-safe).")

            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "quantity": format_qty(self.step_size, executed_qty), "stopPrice": format_price(self.tick_size, sl_price),
                "reduceOnly": "true"
            })

            remaining = Decimal(str(executed_qty))
            for i, tp in enumerate(tp_prices[:num_tps]):
                qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                qty_str = format_qty(self.step_size, qty_dec)

                if i == num_tps - 1 and remaining > 0 and remaining < Decimal(str(self.step_size)):
                    continue

                remaining -= Decimal(qty_str)
                mark_price = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
                tp_f = float(tp)

                if (side == SIDE_BUY and tp_f <= mark_price) or (side == SIDE_SELL and tp_f >= mark_price):
                    batch.append({"symbol": self.symbol, "side": sl_side, "type": ORDER_TYPE_MARKET, "quantity": qty_str, "reduceOnly": "true"})
                else:
                    batch.append({"symbol": self.symbol, "side": sl_side, "type": TAKE_PROFIT_MARKET, "quantity": qty_str, "stopPrice": format_price(self.tick_size, tp_f), "reduceOnly": "true"})

            results = await self.client.futures_place_batch_order(batchOrders=batch)
            logging.info(f"SL/TP batch response: {results}")

            if results and len(results) > 0 and "orderId" in results[0]:
                sl_order_id = results[0]["orderId"]
                logging.info(f"SL Order ID guardado: {sl_order_id}")
            else:
                logging.error("No se pudo obtener el orderId del SL del batch response.")

        except Exception as e:
            logging.error(f"Fallo creando SL/TP: {e}")
            await self.telegram_handler._send_message(f"⚠️ <b>FAIL-SAFE</b>\nFallo SL/TP: {e}")
            await self.close_position_manual(reason="Fallo al crear SL/TP batch")
            return 

        # --- Actualizar el ESTADO ---
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": executed_qty, "entry_price": avg_price,
            "entry_type": entry_type, "mark_price_entry": mark_price_entry,
            "atr_at_entry": self.state.cached_atr, "tps_hit_count": 0,
            "entry_time": time.time(), "sl_order_id": sl_order_id,
            "total_pnl": 0.0, "mark_price": mark_price_entry,
            "unrealized_pnl": 0.0,
        }
        self.state.last_known_position_qty = executed_qty
        self.state.sl_moved_to_be = False
        self.state.trade_cooldown_until = time.time() + 300
        self.save_state()
        # --- Fin de actualizar estado ---

        icon = "🔼" if side == SIDE_BUY else "🔽"
        tp_list_str = ", ".join([format_price(self.tick_size, tp) for tp in tp_prices])
        msg = f"{icon} <b>NUEVA ORDEN: {entry_type}</b> {icon}\n\n" \
              f"<b>Símbolo</b>: <code>{self.symbol}</code>\n" \
              f"<b>Lado</b>: <code>{side}</code>\n" \
              f"<b>Cantidad</b>: <code>{format_qty(self.step_size, executed_qty)}</code>\n" \
              f"<b>Entrada</b>: <code>{format_price(self.tick_size, avg_price)}</code>\n" \
              f"<b>SL</b>: <code>{format_price(self.tick_size, sl_price)}</code> (ID: {sl_order_id})\n" \
              f"<b>TPs</b>: <code>{tp_list_str}</code>\n" \
              f"<b>ATR en Entrada</b>: <code>{self.state.cached_atr:.2f if self.state.cached_atr else 'N/A'}</code>\n"
        await self.telegram_handler._send_message(msg)

    async def _move_sl_to_be(self, remaining_qty_float):
        if self.state.sl_moved_to_be: return
        logging.info("Moviendo SL a Break-Even (disparado por TP2)...")
        try:
            entry_price = self.state.current_position_info.get("entry_price")
            side = self.state.current_position_info.get("side")
            old_sl_id = self.state.current_position_info.get("sl_order_id")

            if not entry_price or not side:
                logging.warning("No se puede mover SL a BE, falta info de entrada.")
                return

            if old_sl_id:
                try:
                    await self.client.futures_cancel_order(symbol=self.symbol, orderId=old_sl_id)
                    logging.info(f"Antiguo SL (ID: {old_sl_id}) cancelado.")
                except BinanceAPIException as e:
                    if e.code == -2011: logging.warning("SL antiguo ya no existía.")
                    else: raise e
            else:
                logging.warning("No se encontró old_sl_id para cancelar.")

            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            new_sl_order = await self.client.futures_create_order(
                symbol=self.symbol, side=sl_side, type=STOP_MARKET,
                quantity=format_qty(self.step_size, remaining_qty_float),
                stopPrice=format_price(self.tick_size, entry_price),
                reduceOnly="true"
            )

            new_sl_id = new_sl_order.get("orderId")
            self.state.sl_moved_to_be = True
            self.state.current_position_info["sl_order_id"] = new_sl_id
            self.save_state()
            await self.telegram_handler._send_message(f"🛡️ <b>TP2 ALCANZADO</b>\nSL movido a BE: <code>{format_price(self.tick_size, entry_price)}</code> (ID: {new_sl_id})")

        except Exception as e:
            logging.error(f"Error moviendo SL a BE: {e}")
            await self.telegram_handler._send_message("⚠️ Error al mover SL a Break-Even.")

    async def close_position_manual(self, reason="Manual Close"):
        logging.warning(f"Cerrando posición manualmente: {reason}")
        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.symbol)
            pos = await self._get_current_position()
            qty = float(pos.get("positionAmt", 0))

            if qty == 0:
                logging.info("Intento de cierre manual, pero la posición ya es 0.")
                if self.state.is_in_position:
                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.last_known_position_qty = 0.0
                    self.state.sl_moved_to_be = False
                    self.save_state()
                return

            close_side = SIDE_SELL if qty > 0 else SIDE_BUY
            await self.client.futures_create_order(
                symbol=self.symbol, side=close_side, type=ORDER_TYPE_MARKET,
                quantity=format_qty(self.step_size, abs(qty)),
                reduceOnly="true"
            )
            logging.info(f"Orden MARKET de cierre enviada. Razón: {reason}")
        except Exception as e:
            logging.error(f"Error en _close_position_manual: {e}")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR</b>\nFallo al intentar cierre manual ({reason}).")

    # --- Lógica de Riesgo/Estrategia (Se moverá a risk.py) ---

    async def seek_new_trade(self, kline):
        if self.state.trading_paused: return
        if time.time() < self.state.trade_cooldown_until: return
        if not self.state.daily_pivots: return
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            logging.debug("Indicators not ready")
            return

        async with self.lock:
            if self.state.is_in_position: return

            current_price = float(kline["c"])
            current_volume = float(kline["q"])

            median_vol = self.state.cached_median_vol
            if not median_vol or median_vol == 0:
                logging.debug("median vol (1m, USDT) es 0 o None")
                return

            required_volume = median_vol * self.volume_factor
            volume_confirmed = current_volume > required_volume

            p = self.state.daily_pivots
            atr = self.state.cached_atr
            ema = self.state.cached_ema
            side, entry_type, sl, tp_prices = None, None, None, []

            if current_price > p["H4"]:
                if volume_confirmed and current_price > ema:
                    side, entry_type = SIDE_BUY, "Breakout Long"
                    sl = current_price - atr * self.breakout_atr_sl_multiplier
                    tp_prices = [current_price + atr * self.breakout_tp_mult]
                else:
                    logging.info(f"[DEBUG H4] Rechazado. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f}), EMA: {current_price > ema}")

            elif current_price < p["L4"]:
                if volume_confirmed and current_price < ema:
                    side, entry_type = SIDE_SELL, "Breakout Short"
                    sl = current_price + atr * self.breakout_atr_sl_multiplier
                    tp_prices = [current_price - atr * self.breakout_tp_mult]
                else:
                    logging.info(f"[DEBUG L4] Rechazado. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f}), EMA: {current_price < ema}")

            elif current_price <= p["L3"]:
                if volume_confirmed:
                    side, entry_type = SIDE_BUY, "Ranging Long"
                    sl = p["L4"] - atr * self.ranging_atr_multiplier
                    tp_prices = [p["P"], p["H1"], p["H2"]]
                else:
                    logging.info(f"[DEBUG L3] Rechazado. Precio OK. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f})")

            elif current_price >= p["H3"]:
                if volume_confirmed:
                    side, entry_type = SIDE_SELL, "Ranging Short"
                    sl = p["H4"] + atr * self.ranging_atr_multiplier
                    tp_prices = [p["P"], p["L1"], p["L2"]]
                else:
                    logging.info(f"[DEBUG H3] Rechazado. Precio OK. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f})")

            if side:
                balance = await self._get_account_balance()
                if balance is None: return
                if await self._daily_loss_exceeded(balance):
                    await self.telegram_handler._send_message("❌ <b>Daily loss limit reached</b> — trading paused.")
                    self.state.trade_cooldown_until = time.time() + 86400
                    return

                invest = balance * self.investment_pct
                qty = float(format_qty(self.step_size, (invest * self.leverage) / current_price))
                if qty <= 0:
                    logging.warning("Qty computed 0; skip")
                    return

                if entry_type.startswith("Breakout"):
                    tp_prices = [tp_prices[0]]

                tp_prices_fmt = [float(format_price(self.tick_size, tp)) for tp in tp_prices if tp is not None]

                logging.info(f"!!! SEÑAL !!! {entry_type} {side} ; qty {qty} ; SL {sl} ; TPs {tp_prices_fmt}")
                await self._place_bracket_order(side, qty, current_price, sl, tp_prices_fmt, entry_type)

    async def _daily_loss_exceeded(self, balance):
        total_pnl = self.state.current_position_info.get("total_pnl", 0)
        total_pnl += sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        loss_limit = -abs((self.daily_loss_limit_pct / 100.0) * balance)
        return total_pnl <= loss_limit

    # --- Lógica de Streams (Se moverá a streams.py) ---

    async def handle_kline_evt(self, msg):
        if not msg: return
        if msg.get("e") == "error":
            logging.error(f"WS error event: {msg}")
            return
        k = msg.get("k", {})
        if not k.get("x", False): return
        if not self.state.is_in_position:
            await self.seek_new_trade(k)

    async def check_position_state(self):
        async with self.lock:
            try:
                pos = await self._get_current_position()
                if not pos: return

                qty = abs(float(pos.get("positionAmt", 0)))

                if not self.state.is_in_position:
                    if qty > 0:
                        logging.info("Detected open position by poll; syncing state")
                        self.state.is_in_position = True
                        self.state.current_position_info = {
                            "quantity": qty, "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0, "entry_time": time.time(), "total_pnl": 0.0,
                            "mark_price": float(pos.get("markPrice", 0.0)),
                            "unrealized_pnl": float(pos.get("unRealizedProfit", 0.0)),
                        }
                        self.state.last_known_position_qty = qty
                        await self.telegram_handler._send_message("🔁 Posición detectada por poll; bot sincronizado.")
                        self.save_state()
                    return 

                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice", 0.0))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit", 0.0))

                if qty == 0:
                    logging.info("Posición cerrada detectada (qty 0).")
                    pnl, close_px, roi = 0.0, 0.0, 0.0
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.symbol, limit=1))[0]
                        pnl = float(last_trade.get("realizedPnl", 0.0))
                        close_px = float(last_trade.get("price", 0.0))
                    except Exception as e:
                        logging.error(f"Error al obtener último trade para PnL: {e}")

                    total_pnl = self.state.current_position_info.get("total_pnl", 0) + pnl
                    entry_price = self.state.current_position_info.get("entry_price", 0.0)
                    quantity = self.state.current_position_info.get("quantity", 0.0)

                    if entry_price > 0 and quantity > 0 and self.leverage > 0:
                        initial_margin = (entry_price * quantity) / self.leverage
                        if initial_margin > 0:
                            roi = (total_pnl / initial_margin) * 100

                    td = {
                        "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_type": self.state.current_position_info.get("entry_type", "Unknown"),
                        "side": self.state.current_position_info.get("side", "Unknown"),
                        "quantity": quantity, "entry_price": entry_price,
                        "mark_price_entry": self.state.current_position_info.get("mark_price_entry", 0.0),
                        "close_price_avg": close_px, "pnl": total_pnl, "pnl_percent_roi": roi, 
                        "cpr_width": self.state.daily_pivots.get("width", 0),
                        "atr_at_entry": self.state.current_position_info.get("atr_at_entry", 0),
                        "ema_filter": self.state.current_position_info.get("ema_at_entry", 0)
                    }
                    self._log_trade_to_csv(td)
                    self.state.daily_trade_stats.append({"pnl": total_pnl, "roi": roi})

                    icon = "✅" if total_pnl >= 0 else "❌"
                    msg = f"{icon} <b>POSICIÓN CERRADA</b> {icon}\n\n" \
                          f"<b>Tipo</b>: <code>{self.state.current_position_info.get('entry_type', 'N/A')}</code>\n" \
                          f"<b>PnL Total</b>: <code>{total_pnl:+.2f} USDT</code>\n" \
                          f"<b>ROI</b>: <code>{roi:+.2f}%</code> (sobre margen inicial)\n"
                    await self.telegram_handler._send_message(msg)

                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.last_known_position_qty = 0.0
                    self.state.sl_moved_to_be = False
                    self.save_state()
                    return 

                if qty < self.state.last_known_position_qty:
                    partial_pnl = 0.0
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.symbol, limit=1))[0]
                        partial_pnl = float(last_trade.get("realizedPnl", 0.0))
                    except Exception: pass

                    tp_hit_count = self.state.current_position_info.get("tps_hit_count", 0) + 1
                    self.state.current_position_info["tps_hit_count"] = tp_hit_count
                    self.state.current_position_info["total_pnl"] = self.state.current_position_info.get("total_pnl", 0) + partial_pnl

                    logging.info(f"TP PARCIAL ALCANZADO (TP{tp_hit_count}). Qty restante: {qty}. PnL: {partial_pnl}")
                    await self.telegram_handler._send_message(f"🎯 <b>TP{tp_hit_count} ALCANZADO</b>\nPnL: <code>{partial_pnl:+.2f}</code> | Qty restante: {qty}")

                    self.state.last_known_position_qty = qty
                    self.save_state()

                    if tp_hit_count == 2 and not self.state.sl_moved_to_be:
                        await self._move_sl_to_be(qty)

                if (not self.state.sl_moved_to_be and 
                    self.state.current_position_info.get("entry_type", "").startswith("Ranging")):

                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0 and (time.time() - entry_time) / 3600 > 6:
                        logging.warning(f"TIME STOP (6h) triggered for Ranging trade. Closing position.")
                        await self.telegram_handler._send_message(f"⏳ <b>CIERRE POR TIEMPO</b>\nTrade de Rango superó 6h. Cerrando.")
                        await self.close_position_manual(reason="Time Stop 6h")

            except BinanceAPIException as e:
                if e.code == -1003: logging.warning("Rate limit (-1003) en check_position_state.")
                else: logging.error(f"Error de API en check_position_state: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"Error en check_position_state: {e}", exc_info=True)

    async def account_poller_loop(self):
        logging.info("Poller de cuenta iniciado (intervalo %.1fs)", self.account_poll_interval)
        while self.running:
            await self.check_position_state()
            await asyncio.sleep(self.account_poll_interval)

    async def run_user_data_loop(self):
        logging.info("User Data Stream (UDS) conectando...")
        while self.running:
            try:
                async with self.bsm.futures_user_socket() as user_socket:
                    logging.info("User Data Stream (UDS) conectado.")
                    while self.running:
                        msg = await user_socket.recv()
                        if msg:
                            asyncio.create_task(self._handle_user_data_message(msg))
            except Exception as e:
                logging.error(f"Error en User Data Stream (UDS): {e}. Reconectando en 5s...")
                await self.telegram_handler._send_message("🔌 <b>ALERTA UDS</b>\nStream de usuario desconectado. Reconectando...")
                await asyncio.sleep(5)

    async def _handle_user_data_message(self, msg):
        try:
            event_type = msg.get('e')
            if event_type == 'ORDER_TRADE_UPDATE':
                order_data = msg.get('o', {})
                if (order_data.get('s') == self.symbol and 
                    order_data.get('X') == 'FILLED'):
                    order_type = order_data.get('o')
                    if order_type in [STOP_MARKET, TAKE_PROFIT_MARKET]:
                        logging.info(f"UDS: ¡Evento de {order_type} detectado! Forzando chequeo de posición.")
                        await self.check_position_state()
        except Exception as e:
            logging.error(f"Error al manejar mensaje de UDS: {e}", exc_info=True)

    def _log_trade_to_csv(self, trade_data):
        file_exists = os.path.isfile(CSV_FILE)
        try:
            with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
                import csv as _csv
                writer = _csv.DictWriter(f, fieldnames=CSV_HEADER)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(trade_data)
            logging.info("Trade cerrado guardado en CSV.")
        except Exception as e:
            logging.error(f"Error al guardar CSV: {e}")

    async def timed_tasks_loop(self):
        logging.info("Timed tasks loop started")

        if self.state.daily_start_balance is None:
             self.state.daily_start_balance = await self._get_account_balance()
             self.state.start_of_day = datetime.utcnow().date()
             logging.info(f"Balance inicial de {self.state.start_of_day} seteado: {self.state.daily_start_balance}")

        await asyncio.gather(self.calculate_pivots(), self.update_indicators())
        last_indicator_update = datetime.utcnow()

        while self.running:
            try:
                now = datetime.utcnow()

                if now.time() >= dt_time(0, 1) and now.date() > self.state.start_of_day:
                    logging.info("--- NUEVO DÍA UTC ---")
                    self.state.daily_start_balance = await self._get_account_balance()
                    self.state.daily_trade_stats = []
                    self.state.start_of_day = now.date()
                    logging.info(f"Balance de inicio de día {self.state.start_of_day} seteado: {self.state.daily_start_balance}")
                    self.save_state()

                if now.time() >= dt_time(0, 2) and (self.state.last_pivots_date is None or now.date() > self.state.last_pivots_date):
                    await self.calculate_pivots()

                if (now - last_indicator_update).total_seconds() >= self.indicator_update_interval_minutes * 60:
                    await self.update_indicators()
                    last_indicator_update = now

                await asyncio.sleep(10)
            except Exception as e:
                logging.error(f"Timed tasks error: {e}")
                await asyncio.sleep(10)

    # -------------- START / RUN --------------
    async def run(self):
        logging.info(f"Iniciando bot asíncrono v70...")
        if TESTNET_MODE:
            logging.warning("¡¡¡ ATENCIÓN: v70 corriendo en MODO TESTNET !!!")

        self.client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET_MODE)
        self.bsm = BinanceSocketManager(self.client)
        await self._get_exchange_info()
        self.load_state() # Carga el estado en self.state

        if not TESTNET_MODE:
            try:
                pos = await self._get_current_position()
                if pos and float(pos.get("positionAmt", 0)) != 0:
                    logging.warning("Reconciliación: posición activa encontrada, sincronizando.")
                    self.state.is_in_position = True
                    if not self.state.current_position_info:
                        self.state.current_position_info = {
                            "quantity": abs(float(pos["positionAmt"])),
                            "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0, "entry_time": time.time(), "total_pnl": 0.0,
                        }
                    self.state.last_known_position_qty = abs(float(pos["positionAmt"]))
                    await self.telegram_handler._send_message("🤖 Bot reiniciado y reconciliado: posición activa encontrada.")
                    self.save_state()
                else:
                    logging.info("No active position on reconcile.")
            except Exception as e:
                logging.error(f"Error during reconcile: {e}")
        else:
             logging.info("Modo Testnet: reconciliación de posiciones omitida.")

        self.running = True
        tasks = []
        tasks.append(asyncio.create_task(self.timed_tasks_loop()))
        tasks.append(asyncio.create_task(self.account_poller_loop()))
        tasks.append(asyncio.create_task(self.telegram_handler.start_polling())) # <-- Tarea de Telegram
        tasks.append(asyncio.create_task(self.run_user_data_loop()))

        logging.info("Connecting WS (Klines) 1m...")
        stream_ctx = self.bsm.kline_socket(symbol=self.symbol.lower(), interval="1m")

        try:
            async with stream_ctx as ksocket:
                logging.info("WS (Klines) conectado, escuchando 1m klines...")
                while self.running:
                    try:
                        msg = await ksocket.recv() 
                        if msg:
                            asyncio.create_task(self.handle_kline_evt(msg))
                    except Exception as e:
                        logging.error(f"WS (Klines) recv/handle error: {e}")
                        await self.telegram_handler._send_message("🚨 <b>WS KLINE ERROR</b>\nReiniciando conexión.")
                        await asyncio.sleep(5)
                        break 
        except Exception as e:
            logging.critical(f"WS (Klines) fatal connection error: {e}")
            await self.telegram_handler._send_message("🚨 <b>WS KLINE FATAL ERROR</b>\nRevisar logs.")

        finally:
            logging.warning("Saliendo del bucle WS (Klines). Iniciando apagado...")
            self.running = False
            for t in tasks:
                t.cancel()

    async def shutdown(self):
        logging.warning("Shutdown recibido. Guardando estado.")
        self.running = False
        self.save_state()
        try:
            await self.telegram_handler.stop()
        except Exception: pass
        try:
            if self.client:
                await self.client.close_connection()
        except Exception: pass
        logging.info("Estado guardado atómicamente. Saliendo.")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)

# -------------- Entrypoint --------------
async def main():
    bot = BotControllerV70()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.shutdown()))
        except Exception:
            pass
    await bot.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Cierre manual detectado (KeyboardInterrupt).")
    except SystemExit:
        logging.info("Bot finalizado.")
    except Exception as e:
        logging.critical(f"Error fatal nivel superior: {e}", exc_info=True)
