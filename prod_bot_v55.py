#!/usr/bin/env python3
# prod_bot_v55.py
# Versión: v55 (async, optimized, telegram interactive, testnet by default)
# NO usar claves en el código. Use variables de entorno.

import os
import sys
import time
import json
import shutil
import asyncio
import logging
import signal
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta, time as dt_time
from logging.handlers import RotatingFileHandler

# Binances async client
from binance import AsyncClient, BinanceSocketManager
from binance.exceptions import BinanceAPIException

# Async HTTP for Telegram and any external REST
import httpx

# Retries
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

# --- CONFIG --- (constants)
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
ORDER_TYPE_MARKET = "MARKET"
STOP_MARKET = "STOP_MARKET"
TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"

# CSV / state header
CSV_HEADER = [
    "timestamp_utc", "entry_type", "side", "quantity", "entry_price", "mark_price_entry",
    "close_price_avg", "pnl", "pnl_percent_roi", "cpr_width", "atr_at_entry", "ema_filter"
]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_FILE = os.path.join(LOG_DIR, "trading_bot_v55.log")
STATE_FILE = os.path.join(BASE_DIR, "bot_state_v55.json")
CSV_FILE = os.path.join(DATA_DIR, "trades_log_v55.csv")

os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# Logging
log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
rot_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5)
rot_handler.setFormatter(log_formatter)
console = logging.StreamHandler()
console.setFormatter(log_formatter)
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logger.addHandler(rot_handler)
logger.addHandler(console)

# Read env vars
API_KEY = os.environ.get("BINANCE_TESTNET_API_KEY")
API_SECRET = os.environ.get("BINANCE_TESTNET_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TESTNET_MODE = os.environ.get("TESTNET_MODE", "true").lower() in ("1", "true", "yes")
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "5.0"))

if not API_KEY or not API_SECRET:
    logging.critical("Falta BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_SECRET_KEY en las ENV")
    # We'll still allow import for code review, but runtime will raise
# Telegram minimal check (optional)
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logging.warning("Variables de Telegram no configuradas. El bot seguirá funcionando pero sin notificaciones.")

# Tenacity decorator for async functions that call external API
def tenacity_retry_decorator_async():
    return retry(
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((httpx.RequestError, BinanceAPIException)),
        reraise=True,
    )

# Utility formatting helpers (tick and step sizes will come from exchange info)
def quantize_decimal(value, quantum):
    return str(Decimal(str(value)).quantize(Decimal(str(quantum)), rounding=ROUND_DOWN))

# --- Bot class ---
class AsyncTradingBotV55:
    def __init__(
        self,
        symbol="BTCUSDT",
        investment_pct=0.05,
        leverage=30,
        cpr_width_threshold=0.2,
        volume_factor=1.5,
        take_profit_levels=3,
        atr_period=14,
        ranging_atr_mult=0.5,
        breakout_atr_sl_mult=1.0,
        breakout_tp_mult=1.25,  # breakout TP multiplier default 1.25×ATR (user asked)
        range_tp_mult=2.0,      # range TP multiplier default 2×ATR
        ema_period=50,
        ema_timeframe="1h",
    ):
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

        # Binance client & ws manager (created in run)
        self.client = None
        self.bsm = None

        # Tick/step size
        self.tick_size = None
        self.step_size = None

        # Cached indicators
        self.cached_atr = None
        self.cached_ema = None
        self.cached_avg_vol = None

        # Pivots
        self.daily_pivots = {}
        self.last_pivots_date = None

        # Position state
        self.is_in_position = False
        self.current_position_info = {}
        self.last_known_position_qty = 0.0
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0

        # Stats
        self.daily_trade_stats = []
        self.start_of_day = datetime.utcnow().date()

        # Lock for concurrent trading actions
        self.lock = asyncio.Lock()

        # httpx client for Telegram and other REST
        self.httpx_client = httpx.AsyncClient(timeout=10.0)

        # internal control
        self.running = True

        # small config
        self.account_poll_interval = 2.0  # poll account every 2s (light)
        self.indicator_update_interval_minutes = 15

        # Telegram
        self.telegram_token = TELEGRAM_BOT_TOKEN
        self.telegram_chat = TELEGRAM_CHAT_ID
        self.telegram_offset = None  # for getUpdates

    # -------------- STATE PERSISTENCE --------------
    def _sanitize_for_json(self, data):
        if isinstance(data, dict):
            return {k: self._sanitize_for_json(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._sanitize_for_json(x) for x in data]
        if isinstance(data, Decimal):
            return float(data)
        try:
            import numpy as np
            if isinstance(data, (np.floating, float)):
                return float(data)
        except Exception:
            pass
        return data

    def save_state(self):
        state = {
            "is_in_position": self.is_in_position,
            "current_position_info": self._sanitize_for_json(self.current_position_info),
            "sl_moved_to_be": self.sl_moved_to_be,
            "last_known_position_qty": float(self.last_known_position_qty),
            "trade_cooldown_until": self.trade_cooldown_until,
            "daily_trade_stats": self.daily_trade_stats,
            "last_pivots_date": str(self.last_pivots_date) if self.last_pivots_date else None,
            "cached_atr": float(self.cached_atr) if self.cached_atr else None,
            "cached_ema": float(self.cached_ema) if self.cached_ema else None,
        }
        tmp = STATE_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            shutil.move(tmp, STATE_FILE)
            logging.info("Estado guardado atómicamente.")
        except Exception as e:
            logging.error("Error guardando estado: %s", e)

    def load_state(self):
        if not os.path.exists(STATE_FILE):
            logging.info("No state file, iniciando limpio.")
            return
        try:
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
            self.is_in_position = state.get("is_in_position", False)
            self.current_position_info = state.get("current_position_info", {})
            self.sl_moved_to_be = state.get("sl_moved_to_be", False)
            self.last_known_position_qty = state.get("last_known_position_qty", 0.0)
            self.trade_cooldown_until = state.get("trade_cooldown_until", 0)
            self.daily_trade_stats = state.get("daily_trade_stats", [])
            lp = state.get("last_pivots_date")
            self.last_pivots_date = datetime.fromisoformat(lp).date() if lp else None
            self.cached_atr = state.get("cached_atr")
            self.cached_ema = state.get("cached_ema")
            logging.info("Estado cargado: %s", {k: state.get(k) for k in ("is_in_position", "last_known_position_qty", "last_pivots_date")})
        except Exception as e:
            logging.error("Error cargando estado, iniciando limpio: %s", e)

    # -------------- TELEGRAM (async httpx) --------------
    async def _tg_send(self, text):
        if not self.telegram_token or not self.telegram_chat:
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat, "text": text, "parse_mode": "HTML"}
        try:
            await self.httpx_client.post(url, json=payload)
        except Exception as e:
            logging.error("Error enviando Telegram: %s", e)

    async def _tg_get_updates(self):
        if not self.telegram_token:
            return []
        url = f"https://api.telegram.org/bot{self.telegram_token}/getUpdates"
        params = {"timeout": 1, "offset": self.telegram_offset}
        try:
            r = await self.httpx_client.get(url, params=params)
            j = r.json()
            if j.get("ok"):
                updates = j.get("result", [])
                return updates
        except Exception as e:
            logging.error("Error getUpdates: %s", e)
        return []

    async def telegram_poll_loop(self):
        # Polling for commands every 2-3 seconds
        logging.info("Telegram poll loop started")
        while self.running:
            try:
                updates = await self._tg_get_updates()
                for u in updates:
                    self.telegram_offset = u["update_id"] + 1
                    if "message" in u:
                        await self._handle_telegram_message(u["message"])
            except Exception as e:
                logging.error("Telegram loop error: %s", e)
            await asyncio.sleep(2)

    async def _handle_telegram_message(self, msg):
        try:
            text = msg.get("text", "")
            chat_id = str(msg["chat"]["id"])
            # only accept commands from configured chat (optional safety)
            if self.telegram_chat and chat_id != str(self.telegram_chat):
                logging.info("Telegram message from non-authorized chat %s ignored", chat_id)
                return
            if text.startswith("/status"):
                await self._tg_send(self._status_text())
            elif text.startswith("/pivots"):
                await self._tg_send(self._pivots_text())
            elif text.startswith("/kill"):
                await self._tg_send("Bot shutting down by command..."); await self.shutdown()
            elif text.startswith("/restart"):
                await self._tg_send("Restart requested (will stop the service)."); await self.shutdown()
            elif text.startswith("/limit"):
                await self._tg_send(f"Daily loss limit: {DAILY_LOSS_LIMIT_PCT}%")
            elif text.startswith("/testnet_on"):
                await self._tg_send("Testnet mode toggling is manual in ENV; restart required.")
            else:
                await self._tg_send("Comando no reconocido. /status /pivots /kill /restart /limit")
        except Exception as e:
            logging.error("Error handling telegram message: %s", e)

    # --- INICIO: Mensaje /status MEJORADO ---
    def _status_text(self):
        s = "<b>🤖 Bot Status</b>\n\n"
        s += f"<b>Símbolo</b>: <code>{self.symbol}</code>\n"

        # Añadir Tipo de Día
        if self.daily_pivots:
            cw = self.daily_pivots.get("width", 0)
            is_ranging = self.daily_pivots.get("is_ranging_day", False)
            day_type = "Rango (Ancho)" if is_ranging else "Tendencia (Estrecho)"
            s += f"<b>Tipo de Día</b>: <code>{day_type} (CPR: {cw:.2f}%)</code>\n"
        else:
            s += "<b>Tipo de Día</b>: <code>Calculando...</code>\n"

        s += f"<b>Posición</b>: <code>{'EN POSICIÓN' if self.is_in_position else 'Sin posición'}</code>\n"
        if self.is_in_position:
            side = self.current_position_info.get('side')
            icon = "🔼" if side == "BUY" else "🔽"
            s += f"  {icon} <b>Lado</b>: <code>{side}</code>\n"
            s += f"  <b>Qty</b>: <code>{self.current_position_info.get('quantity')}</code>\n"
            s += f"  <b>Entrada</b>: <code>{self.current_position_info.get('entry_price')}</code>\n"

        s += "\n<b>Indicadores</b>\n"
        s += f"  <b>ATR(1h)</b>: <code>{self.cached_atr:.2f}</code>\n"
        s += f"  <b>EMA({self.ema_period})</b>: <code>{self.cached_ema:.2f}</code>\n"
        
        s += "\n<b>Gestión de Riesgo</b>\n"
        pnl_diario = sum(t.get("pnl", 0) for t in self.daily_trade_stats)
        s += f"  <b>PnL Hoy</b>: <code>{pnl_diario:.2f} USDT</code>\n"
        s += f"  <b>Límite Pérdida</b>: <code>{DAILY_LOSS_LIMIT_PCT}%</code>\n"
        
        return s
    # --- FIN: Mensaje /status MEJORADO ---

    # --- INICIO: Mensaje /pivots MEJORADO (CON H1-H6) ---
    def _pivots_text(self):
        if not self.daily_pivots:
            return "📐 Pivotes no calculados aún."
        
        s = "<b>📐 Pivotes Camarilla (Clásica)</b>\n\n"
        
        # Info de CPR
        cw = self.daily_pivots.get("width", 0)
        is_ranging = self.daily_pivots.get("is_ranging_day", False)
        day_type = "Rango (CPR Ancho)" if is_ranging else "Tendencia (CPR Estrecho)"
        s += f"<b>Análisis CPR: {day_type}</b> ({cw:.2f}%)\n"
        s += f"  TC: <code>{self.daily_pivots.get('TC')}</code>\n"
        s += f"  P:  <code>{self.daily_pivots.get('P')}</code>\n"
        s += f"  BC: <code>{self.daily_pivots.get('BC')}</code>\n\n"
        
        s += "<b>Niveles Resistencia (H)</b>\n"
        s += f"  H6 (Target): <code>{self.daily_pivots.get('H6')}</code>\n"
        s += f"  H5 (Target): <code>{self.daily_pivots.get('H5')}</code>\n"
        s += f"  H4 (Breakout): <code>{self.daily_pivots.get('H4')}</code>\n"
        s += f"  H3 (Rango): <code>{self.daily_pivots.get('H3')}</code>\n\n"
        
        s += "<b>Niveles Soporte (L)</b>\n"
        s += f"  L3 (Rango): <code>{self.daily_pivots.get('L3')}</code>\n"
        s += f"  L4 (Breakout): <code>{self.daily_pivots.get('L4')}</code>\n"
        s += f"  L5 (Target): <code>{self.daily_pivots.get('L5')}</code>\n"
        s += f"  L6 (Target): <code>{self.daily_pivots.get('L6')}</code>\n"
        
        return s
    # --- FIN: Mensaje /pivots MEJORADO ---

    # -------------- BINANCE INFO & INDICATORS --------------
    @tenacity_retry_decorator_async()
    async def _get_exchange_info(self):
        info = await self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == self.symbol:
                filters = {f["filterType"]: f for f in s["filters"]}
                self.tick_size = Decimal(filters["PRICE_FILTER"]["tickSize"])
                self.step_size = Decimal(filters["LOT_SIZE"]["stepSize"])
                logging.info("Reglas %s: Tick %s, Step %s", self.symbol, self.tick_size, self.step_size)
                return
        raise Exception("Symbol not found in exchange info")

    def _format_price(self, p):
        try:
            if self.tick_size:
                return str(Decimal(str(p)).quantize(self.tick_size, rounding=ROUND_DOWN))
        except Exception:
            pass
        return f"{float(p):.8f}"

    def _format_qty(self, q):
        try:
            if self.step_size:
                return str(Decimal(str(q)).quantize(self.step_size, rounding=ROUND_DOWN))
        except Exception:
            pass
        return f"{float(q):.8f}"

    @tenacity_retry_decorator_async()
    async def _get_klines(self, interval="1h", limit=50):
        kl = await self.client.futures_klines(symbol=self.symbol, interval=interval, limit=limit)
        return kl

    async def update_indicators(self):
        try:
            # ATR (manual)
            kl = await self._get_klines(interval="1h", limit=50)
            highs = [float(k[2]) for k in kl]
            lows = [float(k[3]) for k in kl]
            closes = [float(k[4]) for k in kl]
            trs = []
            for i in range(1, len(kl)):
                tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                trs.append(tr)
            if len(trs) >= self.atr_period:
                first_atr = sum(trs[: self.atr_period]) / self.atr_period
                atr = first_atr
                alpha = 1.0 / self.atr_period
                for tr in trs[self.atr_period :]:
                    atr = (tr * alpha) + (atr * (1 - alpha))
                self.cached_atr = atr
                logging.info("ATR(%d) actualizado: %s", self.atr_period, self.cached_atr)
            # EMA
            kl_ema = await self._get_klines(interval=self.ema_timeframe, limit=max(self.ema_period * 2, 100))
            closes_ema = [float(k[4]) for k in kl_ema]
            if len(closes_ema) >= self.ema_period:
                alpha = 2.0 / (self.ema_period + 1)
                ema = closes_ema[0]
                for price in closes_ema[1:]:
                    ema = (price * alpha) + (ema * (1 - alpha))
                self.cached_ema = ema
                logging.info("EMA(%d) actualizado: %s", self.ema_period, self.cached_ema)
            # Avg volume 1h
            kl_v = await self._get_klines(interval="1h", limit=21)
            volumes = [float(k[5]) for k in kl_v[:-1]]
            if volumes:
                self.cached_avg_vol = sum(volumes) / len(volumes)
                logging.info("AvgVol(1h): %.2f", self.cached_avg_vol)
        except Exception as e:
            logging.error("Error actualizando indicadores: %s", e)

    # --- INICIO: CÁLCULO DE PIVOTES MEJORADO (CON DEBUG Y FÓRMULA CLÁSICA) ---
    @tenacity_retry_decorator_async()
    async def calculate_pivots(self):
        try:
            kl = await self._get_klines(interval="1d", limit=2)
            if len(kl) < 2:
                raise Exception("Insufficient daily klines")
            y = kl[-2]
            
            # --- INICIO: DEBUGGING LOG ---
            k_timestamp = datetime.utcfromtimestamp(y[0] / 1000).strftime('%Y-%m-%d')
            h, l, c = float(y[2]), float(y[3]), float(y[4])
            
            logging.info("-----------------------------------------------")
            logging.info(f"--- DEBUG DATOS DE PIVOTES (Vela de: {k_timestamp}) ---")
            logging.info(f"High (H): {h}")
            logging.info(f"Low (L): {l}")
            logging.info(f"Close (C): {c}")
            logging.info("-----------------------------------------------")
            # --- FIN: DEBUGGING LOG ---

            if l == 0:
                raise Exception("Daily low zero")

            # --- INICIO: Cálculos Clásicos de Camarilla (Tu fórmula) ---
            piv = (h + l + c) / 3.0
            rng = h - l

            # Niveles Rango (1-4)
            r4 = c + (h - l) * 1.1 / 2
            r3 = c + (h - l) * 1.1 / 4
            r2 = c + (h - l) * 1.1 / 6
            r1 = c + (h - l) * 1.1 / 12
            s1 = c - (h - l) * 1.1 / 12
            s2 = c - (h - l) * 1.1 / 6
            s3 = c - (h - l) * 1.1 / 4
            s4 = c - (h - l) * 1.1 / 2

            # Niveles Target (5-6)
            r5 = (h / l) * c
            r6 = r5 + 1.168 * (r5 - r4)
            s5 = c - (r5 - c)
            s6 = c - (r6 - c)
            # --- FIN: Cálculos Clásicos de Camarilla ---

            # CPR (sigue siendo útil para el tipo de día)
            bc = (h + l) / 2.0
            tc = (piv - bc) + piv
            cw = abs(tc - bc) / piv * 100 if piv != 0 else 0

            lvls = {
                "P": piv, "BC": bc, "TC": tc,
                "width": cw, "is_ranging_day": cw > self.cpr_width_threshold,
                "H1": r1, "H2": r2, "H3": r3, "H4": r4, "H5": r5, "H6": r6,
                "L1": s1, "L2": s2, "L3": s3, "L4": s4, "L5": s5, "L6": s6,
            }
            
            # (El resto de la función de cuantización)
            newp = {}
            for k, v in lvls.items():
                if k not in ("width", "is_ranging_day"):
                    try:
                        if isinstance(v, (int, float)):
                            if self.tick_size:
                                newp[k] = float(Decimal(str(v)).quantize(self.tick_size, rounding=ROUND_DOWN))
                            else:
                                newp[k] = float(v)
                        else:
                            newp[k] = v
                    except Exception:
                        newp[k] = float(v)
                else:
                    newp[k] = v
                    
            self.daily_pivots = newp
            self.last_pivots_date = datetime.utcnow().date()
            logging.info("Pivotes (Camarilla Clásica) actualizados")
            await self._tg_send(self._pivots_text()) # Enviar pivotes actualizados a Telegram

        except Exception as e:
            logging.error("Error calculating pivots: %s", e)
            if self.daily_pivots:
                logging.warning("Using previous pivots as fallback")
                await self._tg_send("⚠️ <b>ALERTA</b>\nFallo al calcular pivotes. Usando niveles previos.")
            else:
                await self._tg_send("🚨 <b>ERROR</b>\nFallo al calcular pivotes iniciales. Bot inactivo.")
    # --- FIN: CÁLCULO DE PIVOTES MEJORADO ---

    # -------------- ACCOUNT & ORDERS (polling based) --------------
    @tenacity_retry_decorator_async()
    async def _get_account_balance(self):
        info = await self.client.futures_account()
        for a in info.get("assets", []):
            if a.get("asset") == "USDT":
                return float(a.get("walletBalance", 0.0))
        logging.warning("No USDT asset found in account")
        return None

    @tenacity_retry_decorator_async()
    async def _get_current_position(self):
        positions = await self.client.futures_position_information()
        pos = next((p for p in positions if p["symbol"] == self.symbol), None)
        return pos

    @tenacity_retry_decorator_async()
    async def _get_avg_volume_1h(self):
        if self.cached_avg_vol:
            return self.cached_avg_vol
        kl = await self._get_klines(interval="1h", limit=21)
        volumes = [float(k[5]) for k in kl[:-1]]
        if volumes:
            return sum(volumes) / len(volumes)
        return None

    # Place bracket order (market entry + batch SL/TP)
    async def _place_bracket_order(self, side, qty, entry_price_signal, sl_price, tp_prices, entry_type):
        async with self.lock:
            # Send market buy/sell
            try:
                mark_price_entry = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
                logging.info("Enviando MARKET %s %s %s", side, qty, self.symbol)
                market = await self.client.futures_create_order(
                    symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET, quantity=self._format_qty(qty)
                )
            except BinanceAPIException as e:
                logging.error("Market order failed: %s", e)
                await self._tg_send(f"❌ <b>ERROR ENTRY</b>\n{e}")
                self.trade_cooldown_until = time.time() + 300
                return
            # wait for fill (poll)
            filled = False
            attempts = 0
            order_id = market.get("orderId")
            while attempts < 15:
                try:
                    status = await self.client.futures_get_order(symbol=self.symbol, orderId=order_id)
                    if status.get("status") == "FILLED":
                        filled = True
                        avg_price = float(status.get("avgPrice", 0))
                        executed_qty = abs(float(status.get("executedQty", 0)))
                        break
                except Exception:
                    pass
                attempts += 1
                await asyncio.sleep(0.5 + attempts * 0.1)
            if not filled:
                logging.error("Market order not confirmed filled; cooldown set")
                await self._tg_send("❌ <b>ERROR CRÍTICO</b>\nMARKET no confirmado FILLED.")
                self.trade_cooldown_until = time.time() + 300
                return
            # Save state
            self.is_in_position = True
            self.last_order_time = time.time()
            self.current_position_info = {
                "side": side,
                "quantity": executed_qty,
                "entry_price": avg_price,
                "entry_type": entry_type,
                "mark_price_entry": mark_price_entry,
                "atr_at_entry": self.cached_atr,
                "tps_hit_count": 0,
            }
            self.last_known_position_qty = executed_qty
            self.sl_moved_to_be = False
            self.trade_cooldown_until = time.time() + 300
            self.save_state()

            # prepare batch exit orders (SL + TPs)
            try:
                batch = []
                num_tps = min(len(tp_prices), self.take_profit_levels)
                if num_tps == 0:
                    raise Exception("No TP prices")
                tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))
                # SL order (STOP_MARKET reduceOnly)
                sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
                if (side == SIDE_BUY and float(sl_price) >= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])) or \
                   (side == SIDE_SELL and float(sl_price) <= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])):
                    raise Exception("SL already surpassed by market price (fail-safe).")
                batch.append({
                    "symbol": self.symbol,
                    "side": sl_side,
                    "type": STOP_MARKET,
                    "quantity": self._format_qty(executed_qty),
                    "stopPrice": self._format_price(sl_price),
                    "reduceOnly": "true"
                })
                # TPs
                remaining = Decimal(str(executed_qty))
                for i, tp in enumerate(tp_prices[:num_tps]):
                    qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                    qty_str = self._format_qty(qty_dec)
                    remaining -= qty_dec
                    mark_price = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
                    tp_f = float(tp)
                    # if TP would be immediately triggered based on mark price -> market close
                    if (side == SIDE_BUY and tp_f <= mark_price) or (side == SIDE_SELL and tp_f >= mark_price):
                        batch.append({
                            "symbol": self.symbol,
                            "side": sl_side,
                            "type": ORDER_TYPE_MARKET,
                            "quantity": qty_str,
                            "reduceOnly": "true"
                        })
                    else:
                        batch.append({
                            "symbol": self.symbol,
                            "side": sl_side,
                            "type": TAKE_PROFIT_MARKET,
                            "quantity": qty_str,
                            "stopPrice": self._format_price(tp_f),
                            "reduceOnly": "true"
                        })
                # Send batch
                results = await self.client.futures_place_batch_order(batchOrders=batch)
                logging.info("SL/TP batch response: %s", results)
                
                # --- INICIO: Mensaje NUEVA ORDEN MEJORADO ---
                try:
                    # Crear mensaje de Telegram mejorado
                    icon = "🔼" if side == SIDE_BUY else "🔽"
                    tp_list_str = ", ".join([self._format_price(tp) for tp in tp_prices])
                    
                    msg = f"{icon} <b>NUEVA ORDEN: {entry_type}</b> {icon}\n\n"
                    msg += f"<b>Símbolo</b>: <code>{self.symbol}</code>\n"
                    msg += f"<b>Lado</b>: <code>{side}</code>\n"
                    msg += f"<b>Cantidad</b>: <code>{self._format_qty(executed_qty)}</code>\n"
                    msg += f"<b>Entrada</b>: <code>{self._format_price(avg_price)}</code>\n"
                    msg += f"<b>SL</b>: <code>{self._format_price(sl_price)}</code>\n"
                    msg += f"<b>TPs</b>: <code>{tp_list_str}</code>\n"
                    msg += f"<b>ATR en Entrada</b>: <code>{self.cached_atr:.2f}</code>\n"
                    
                    await self._tg_send(msg)
                except Exception as e:
                    logging.error("Fallo enviando Telegram de nueva orden: %s", e)
                # --- FIN: Mensaje NUEVA ORDEN MEJORADO ---
                
            except Exception as e:
                logging.error("Fallo creando SL/TP: %s", e)
                await self._tg_send(f"⚠️ <b>FAIL-SAFE</b>\nFallo SL/TP: {e}")
                # try to close position immediately
                try:
                    pos = await self._get_current_position()
                    if pos and float(pos.get("positionAmt", 0)) != 0:
                        close_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
                        close_qty = abs(float(pos["positionAmt"]))
                        await self.client.futures_create_order(symbol=self.symbol, side=close_side, type=ORDER_TYPE_MARKET, quantity=self._format_qty(close_qty))
                        logging.info("Posición seguridad cerrada.")
                except Exception as e2:
                    logging.critical("FALLO cierre seguridad: %s", e2)
                # reset
                self.is_in_position = False
                self.current_position_info = {}
                self.last_known_position_qty = 0.0
                self.sl_moved_to_be = False
                self.trade_cooldown_until = time.time() + 300
                self.save_state()

    # -------------- CORE STRATEGY: seek_new_trade (called on 1m candle close) --------------
    async def seek_new_trade(self, kline):
        # kline: dict from websocket kline 'k'
        now_ts = time.time()
        if now_ts < self.trade_cooldown_until:
            return
        if not self.daily_pivots:
            logging.debug("No pivots yet")
            return
        if self.cached_atr is None or self.cached_ema is None:
            logging.debug("Indicators not ready")
            return
        async with self.lock:
            try:
                # check if API reports position
                pos = await self._get_current_position()
                if pos and float(pos.get("positionAmt", 0)) != 0:
                    if not self.is_in_position:
                        self.is_in_position = True
                        self.save_state()
                    return
                # extract price & volume from kline
                current_price = float(kline["c"])
                current_volume = float(kline["v"])
                avg_vol = await self._get_avg_volume_1h()
                if not avg_vol:
                    logging.debug("avg vol missing")
                    return
                volume_confirmed = current_volume > (avg_vol * self.volume_factor)
                p = self.daily_pivots
                atr = self.cached_atr
                ema = self.cached_ema
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                # breakout long
                if current_price > p["H4"] and volume_confirmed and current_price > ema:
                    side = SIDE_BUY
                    entry_type = "Breakout Long"
                    sl = current_price - atr * self.breakout_atr_sl_multiplier
                    tp_prices = [current_price + atr * self.breakout_tp_mult]
                # breakout short
                elif current_price < p["L4"] and volume_confirmed and current_price < ema:
                    side = SIDE_SELL
                    entry_type = "Breakout Short"
                    sl = current_price + atr * self.breakout_atr_sl_multiplier
                    tp_prices = [current_price - atr * self.breakout_tp_mult]
                # ranging long
                elif current_price <= p["L3"] and volume_confirmed and current_price > ema:
                    side = SIDE_BUY
                    entry_type = "Ranging Long"
                    sl = p["L4"] - atr * self.ranging_atr_multiplier
                    tp_prices = [p["P"], p["H1"], p["H2"]]
                # ranging short
                elif current_price >= p["H3"] and volume_confirmed and current_price < ema:
                    side = SIDE_SELL
                    entry_type = "Ranging Short"
                    sl = p["H4"] + atr * self.ranging_atr_multiplier
                    tp_prices = [p["P"], p["L1"], p["L2"]]
                # if found signal
                if side:
                    balance = await self._get_account_balance()
                    if balance is None:
                        return
                    # daily loss guard:
                    if await self._daily_loss_exceeded(balance):
                        await self._tg_send("❌ <b>Daily loss limit reached</b> — trading paused for the day.")
                        self.trade_cooldown_until = time.time() + 86400
                        return
                    invest = balance * self.investment_pct
                    qty = float(self._format_qty((invest * self.leverage) / current_price))
                    if qty <= 0:
                        logging.warning("Qty computed 0; skip")
                        return
                    # format TPs (for breakout we keep only first TP as ATR-based)
                    if entry_type.startswith("Breakout"):
                        tp_prices = [current_price + (atr * self.breakout_tp_mult) if side == SIDE_BUY else current_price - (atr * self.breakout_tp_mult)]
                        # optionally add smaller TP if you want multi TP; we keep single for breakouts to avoid distant TP
                    # limit to take_profit_levels
                    tp_prices = tp_prices[: self.take_profit_levels]
                    tp_prices_fmt = [float(self._format_price(tp)) for tp in tp_prices]
                    logging.info("SIGNAL %s %s ; qty %s ; SL %s ; TPs %s", entry_type, side, qty, sl, tp_prices_fmt)
                    # place bracket
                    await self._place_bracket_order(side, qty, current_price, sl, tp_prices_fmt, entry_type)
            except Exception as e:
                logging.error("seek_new_trade error: %s", e)

    # -------------- DAILY LOSS CHECK --------------
    async def _daily_loss_exceeded(self, balance):
        # Sum daily pnl in USD from daily_trade_stats
        # If not tracked, approximate by sum of daily_trade_stats
        pnl = sum(t.get("pnl", 0) for t in self.daily_trade_stats)
        loss_limit = -abs((DAILY_LOSS_LIMIT_PCT / 100.0) * balance)
        return pnl <= loss_limit

    # -------------- KLINE WS HANDLER (1m candles) --------------
    async def handle_kline_evt(self, msg):
        """
        msg is expected to be kline event dictionary (bsm.kline_socket returns similar)
        Structure: msg['k'] has kline fields. We react only on k['x'] True (kline closed)
        """
        if not msg:
            return
        if msg.get("e") == "error":
            logging.error("WS error event: %s", msg)
            return
        k = msg.get("k", {})
        if not k.get("x", False):
            return
        # call seek_new_trade
        await self.seek_new_trade(k)

    # -------------- ACCOUNT POLLER (fast) --------------
    async def account_poller_loop(self):
        logging.info("Account poller started (interval %.1fs)", self.account_poll_interval)
        while self.running:
            try:
                pos = await self._get_current_position()
                if pos:
                    qty = abs(float(pos.get("positionAmt", 0)))
                    if qty != 0 and not self.is_in_position:
                        logging.info("Detected open position by poll; syncing state")
                        self.is_in_position = True
                        self.current_position_info["quantity"] = qty
                        self.current_position_info["entry_price"] = float(pos.get("entryPrice", 0.0))
                        self.last_known_position_qty = qty
                        await self._tg_send("🔁 Posición detectada por poll; bot sincronizado.")
                        self.save_state()
                    
                    # --- INICIO: Bloque CIERRE DE POSICIÓN MEJORADO (CON ROI) ---
                    # detect full close
                    if qty == 0 and self.is_in_position:
                        logging.info("Posición cerrada detectada por poller.")
                        pnl = 0.0
                        close_px = 0.0
                        roi = 0.0
                        
                        # Obtener PnL del último trade
                        try:
                            last_trade = (await self.client.futures_account_trades(symbol=self.symbol, limit=1))[0]
                            pnl = float(last_trade.get("realizedPnl", 0.0))
                            close_px = float(last_trade.get("price", 0.0))
                        except Exception as e:
                            logging.error("Error al obtener último trade para PnL: %s", e)

                        # Calcular ROI
                        entry_price = self.current_position_info.get("entry_price", 0.0)
                        quantity = self.current_position_info.get("quantity", 0.0)
                        
                        if entry_price > 0 and quantity > 0 and self.leverage > 0:
                            initial_margin = (entry_price * quantity) / self.leverage
                            if initial_margin > 0:
                                roi = (pnl / initial_margin) * 100

                        # Guardar CSV (con ROI corregido)
                        td = {
                            "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                            "entry_type": self.current_position_info.get("entry_type", "Unknown"),
                            "side": self.current_position_info.get("side", "Unknown"),
                            "quantity": quantity,
                            "entry_price": entry_price,
                            "mark_price_entry": self.current_position_info.get("mark_price_entry", 0.0),
                            "close_price_avg": close_px,
                            "pnl": pnl,
                            "pnl_percent_roi": roi, # <-- ROI CORREGIDO
                            "cpr_width": self.daily_pivots.get("width", 0),
                            "atr_at_entry": self.current_position_info.get("atr_at_entry", 0),
                            "ema_filter": self.current_position_info.get("ema_at_entry", 0)
                        }
                        self._log_trade_to_csv(td)
                        
                        # Guardar stats diarias
                        self.daily_trade_stats.append({"pnl": pnl, "roi": roi})
                        
                        # Enviar Telegram mejorado
                        icon = "✅" if pnl >= 0 else "❌"
                        msg = (
                            f"{icon} <b>POSICIÓN CERRADA</b> {icon}\n\n"
                            f"<b>Tipo</b>: <code>{self.current_position_info.get('entry_type', 'N/A')}</code>\n"
                            f"<b>PnL</b>: <code>{pnl:+.2f} USDT</code>\n"
                            f"<b>ROI</b>: <code>{roi:+.2f}%</code> (sobre margen inicial)\n"
                        )
                        await self._tg_send(msg)
                        
                        # Reset del estado
                        self.is_in_position = False
                        self.current_position_info = {}
                        self.last_known_position_qty = 0.0
                        self.save_state()
                    # --- FIN: Bloque CIERRE DE POSICIÓN MEJORADO ---
            
            except Exception as e:
                logging.debug("Account poller error: %s", e)
            await asyncio.sleep(self.account_poll_interval)

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
            logging.error("Error al guardar CSV: %s", e)

    # -------------- TIMED TASKS (pivots, indicators, daily summary) --------------
    async def timed_tasks_loop(self):
        logging.info("Timed tasks loop started")
        # initial
        await asyncio.gather(self.calculate_pivots(), self.update_indicators())
        last_indicator_update = datetime.utcnow()
        while self.running:
            try:
                now = datetime.utcnow()
                # pivots at 00:02 UTC (only once per day)
                if now.time() >= dt_time(0, 2) and (self.last_pivots_date is None or now.date() > self.last_pivots_date):
                    await self.calculate_pivots()
                # indicator update every N minutes (aligned)
                if (now - last_indicator_update).total_seconds() >= self.indicator_update_interval_minutes * 60:
                    await asyncio.gather(self.update_indicators())
                    last_indicator_update = now
                # daily summary at 23:56 UTC
                if now.time() >= dt_time(23, 56) and now.time() < dt_time(23, 57):
                    if self.daily_trade_stats:
                        total_trades = len(self.daily_trade_stats)
                        wins = sum(1 for t in self.daily_trade_stats if t.get("pnl", 0) > 0)
                        losses = total_trades - wins
                        total_pnl = sum(t.get("pnl", 0) for t in self.daily_trade_stats)
                        await self._tg_send(f"📊 <b>Resumen Diario</b>\nTrades: {total_trades}\nGanadas: {wins}\nPerdidas: {losses}\nPnL Neto: {total_pnl:+.2f} USDT")
                        self.daily_trade_stats = []
                await asyncio.sleep(10)
            except Exception as e:
                logging.error("Timed tasks error: %s", e)
                await asyncio.sleep(10)

    # -------------- START / RUN --------------
    async def run(self):
        # create client
        logging.info("Iniciando bot asíncrono v55...")
        self.client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET_MODE)
        self.bsm = BinanceSocketManager(self.client)
        # load exchange info
        await self._get_exchange_info()
        # load state
        self.load_state()
        if TESTNET_MODE:
            logging.info("Testnet detectado: se omite reconciliación de posiciones activas.")
        else:
            # reconcile on startup
            try:
                pos = await self._get_current_position()
                if pos and float(pos.get("positionAmt", 0)) != 0:
                    logging.warning("Reconciliación: posición activa encontrada, sincronizando.")
                    self.is_in_position = True
                    self.current_position_info["quantity"] = abs(float(pos["positionAmt"]))
                    self.current_position_info["entry_price"] = float(pos.get("entryPrice", 0.0))
                    self.last_known_position_qty = abs(float(pos["positionAmt"]))
                    await self._tg_send("🤖 Bot reiniciado y reconciliado: posición activa encontrada.")
                    self.save_state()
                else:
                    logging.info("No active position on reconcile.")
            except Exception as e:
                logging.error("Error during reconcile: %s", e)

        # start background tasks
        self.running = True
        tasks = []
        tasks.append(asyncio.create_task(self.timed_tasks_loop()))
        tasks.append(asyncio.create_task(self.account_poller_loop()))
        tasks.append(asyncio.create_task(self.telegram_poll_loop()))

        # --- INICIO: BUCLE DE WEBSOCKET CORREGIDO ---
        logging.info("Connecting WS 1m...")
        # Usamos un 'context manager' para el stream, es más robusto
        stream_ctx = self.bsm.kline_socket(symbol=self.symbol.lower(), interval="1m")

        try:
            # 'async with' maneja la conexión y reconexión
            async with stream_ctx as ksocket:
                logging.info("WS conectado, escuchando 1m klines...")
                
                while self.running: # Bucle principal controlado por nuestro flag
                    try:
                        # FIX 1: Usamos la variable correcta 'ksocket'
                        msg = await ksocket.recv() 
                        if msg:
                            # Creamos una tarea para no bloquear el bucle
                            asyncio.create_task(self.handle_kline_evt(msg))
                    
                    except Exception as e:
                        # Error al recibir/procesar mensaje. Bucle interno.
                        logging.error(f"WS recv/handle error: {e}")
                        await self._tg_send("🚨 <b>WS ERROR INTERNO</b>\nReiniciando conexión.")
                        await asyncio.sleep(5)
                        # Rompemos el bucle interno, 'async with' intentará reconectar
                        break 

        except Exception as e:
            # FIX 2: Indentación corregida. Error fatal que 'async with' no pudo manejar
            logging.critical(f"WS fatal connection error: {e}")
            await self._tg_send("🚨 <b>WS FATAL ERROR</b>\nRevisar logs.")
        
        finally:
            # FIX 2: Indentación corregida.
            # El bucle ha terminado (sea por error o por shutdown)
            logging.warning("Saliendo del bucle WS. Iniciando apagado...")
            self.running = False
            for t in tasks:
                t.cancel()
            
            # El 'await self.shutdown()' se elimina de aquí
            # porque ya es manejado por los 'signal_handler' en main()
        # --- FIN: BUCLE DE WEBSOCKET CORREGIDO ---
           
    async def shutdown(self):
        logging.warning("Shutdown recibido. Guardando estado.")
        self.save_state()
        # close httpx client
        try:
            await self.httpx_client.aclose()
        except Exception:
            pass
        # close binance client
        try:
            if self.client:
                await self.client.close_connection()
        except Exception:
            pass
        logging.info("Estado guardado atómicamente. Saliendo.")
        # exit
        try:
            # allow systemd to detect a clean stop
            sys.exit(0)
        except SystemExit:
            os._exit(0)  # ensure exit

# -------------- Entrypoint --------------
async def main():
    bot = AsyncTradingBotV55()
    # signal handling (set up only in main thread)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.shutdown()))
        except Exception:
            # some environments don't allow add_signal_handler
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
        logging.critical("Error fatal nivel superior: %s", e, exc_info=True)
