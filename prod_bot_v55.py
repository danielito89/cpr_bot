#!/usr/bin/env python3
# prod_bot_v65.py
# Versión: v65 (Fix Crítico: Resuelve Rate Limit Error -1003)
# 1. Elimina la llamada redundante a _get_current_position en seek_new_trade.
# 2. Ralentiza el account_poller_loop de 2s a 5s.

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
LOG_FILE = os.path.join(LOG_DIR, "trading_bot_v65.log") # Log v65
STATE_FILE = os.path.join(BASE_DIR, "bot_state_v65.json") # State v65
CSV_FILE = os.path.join(DATA_DIR, "trades_log_v65.csv") # CSV v65

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

# Silenciar los logs de httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

# Read env vars
API_KEY = os.environ.get("BINANCE_API_KEY") 
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
TESTNET_MODE = os.environ.get("TESTNET_MODE", "false").lower() in ("1", "true", "yes") 

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DAILY_LOSS_LIMIT_PCT = float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "5.0"))

if not API_KEY or not API_SECRET:
    logging.critical("Falta BINANCE_API_KEY/BINANCE_SECRET_KEY en las ENV (para Mainnet)")
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logging.warning("Variables de Telegram no configuradas. El bot seguirá funcionando pero sin notificaciones.")

def tenacity_retry_decorator_async():
    return retry(
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((httpx.RequestError, BinanceAPIException)),
        reraise=True,
    )

def quantize_decimal(value, quantum):
    return str(Decimal(str(value)).quantize(Decimal(str(quantum)), rounding=ROUND_DOWN))

# --- Bot class ---
class AsyncTradingBotV65:
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

        self.client = None
        self.bsm = None
        self.tick_size = None
        self.step_size = None
        self.cached_atr = None
        self.cached_ema = None
        self.cached_median_vol = None 
        self.daily_pivots = {}
        self.last_pivots_date = None
        self.is_in_position = False
        self.current_position_info = {}
        self.last_known_position_qty = 0.0
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0
        self.daily_trade_stats = []
        self.start_of_day = datetime.utcnow().date()
        self.lock = asyncio.Lock()
        self.httpx_client = httpx.AsyncClient(timeout=10.0)
        self.running = True
        # --- CAMBIO v65: Ralentizar el poller ---
        self.account_poll_interval = 5.0  # De 2.0 a 5.0 segundos
        self.indicator_update_interval_minutes = 15
        self.telegram_token = TELEGRAM_BOT_TOKEN
        self.telegram_chat = TELEGRAM_CHAT_ID
        self.telegram_offset = None
        self.trading_paused = False

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
            "trading_paused": self.trading_paused
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
            self.trading_paused = state.get("trading_paused", False)
            logging.info("Estado cargado: %s", {k: state.get(k) for k in ("is_in_position", "last_known_position_qty", "last_pivots_date", "trading_paused")})
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
            if self.telegram_chat and chat_id != str(self.telegram_chat):
                logging.info("Telegram message from non-authorized chat %s ignored", chat_id)
                return
            
            if text.startswith("/status"):
                await self._tg_send(self._status_text())
            
            elif text.startswith("/pivots"):
                await self._tg_send(self._pivots_text())

            elif text.startswith("/pausar"):
                self.trading_paused = True
                self.save_state()
                logging.info("Trading pausado por comando de Telegram.")
                await self._tg_send("⏸️ <b>Bot Pausado</b>\nEl bot no buscará nuevas entradas. Las posiciones abiertas seguirán siendo gestionadas.")

            elif text.startswith("/resumir"):
                self.trading_paused = False
                self.save_state()
                logging.info("Trading reanudado por comando de Telegram.")
                await self._tg_send("▶️ <b>Bot Reanudado</b>\nEl bot vuelve a buscar entradas.")

            elif text.startswith("/cerrar"):
                if not self.is_in_position:
                    await self._tg_send("ℹ️ No hay ninguna posición abierta para cerrar.")
                else:
                    logging.warning("Cierre manual solicitado por Telegram.")
                    await self._tg_send("‼️ <b>Cerrando Posición</b>\nEnviando orden MARKET de cierre...")
                    await self._close_position_manual(reason="Comando /cerrar de Telegram")

            elif text.startswith("/forzar_indicadores"):
                logging.info("Forzando actualización de indicadores...")
                await self._tg_send("⚙️ Forzando actualización de ATR, EMA y VolMedian(USDT)...")
                asyncio.create_task(self.update_indicators())

            elif text.startswith("/forzar_pivotes"):
                logging.info("Forzando recálculo de pivotes...")
                await self._tg_send("📐 Forzando recálculo de Pivotes...")
                asyncio.create_task(self.calculate_pivots())

            elif text.startswith("/limit"):
                await self._tg_send(f"Límite de pérdida diaria: {DAILY_LOSS_LIMIT_PCT}%")

            elif text.startswith("/kill") or text.startswith("/restart"):
                await self._tg_send("🔌 Bot apagándose por comando..."); 
                await self.shutdown()

            else:
                await self._tg_send(
                    "<b>Comando no reconocido.</b>\n"
                    "Comandos disponibles:\n"
                    "<code>/status</code> - Ver estado general\n"
                    "<code>/pivots</code> - Ver pivotes del día\n"
                    "<code>/pausar</code> - Pausar nuevas entradas\n"
                    "<code>/resumir</code> - Reanudar nuevas entradas\n"
                    "<code>/cerrar</code> - Cerrar posición actual\n"
                    "<code>/forzar_indicadores</code> - Recalcular EMA/ATR/Vol\n"
                    "<code>/forzar_pivotes</code> - Recalcular Pivotes\n"
                    "<code>/limit</code> - Ver límite de pérdida\n"
                    "<code>/restart</code> - Reiniciar el bot"
                )
            
        except Exception as e:
            logging.error(f"Error handling telegram message: {e}", exc_info=True)

    def _status_text(self):
        s = "<b>🤖 Bot Status v65 (Mainnet)</b>\n\n"
        
        estado_bot = "🟢 ACTIVO" if not self.trading_paused else "⏸️ PAUSADO"
        s += f"<b>Estado del Bot</b>: <code>{estado_bot}</code>\n"
        s += f"<b>Símbolo</b>: <code>{self.symbol}</code>\n"

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
            s += f"  <b>TPs Alcanzados</b>: <code>{self.current_position_info.get('tps_hit_count', 0)}</code>\n"
            s += f"  <b>SL en BE</b>: <code>{'Sí' if self.sl_moved_to_be else 'No'}</code>\n"

        s += "\n<b>Indicadores</b>\n"
        atr_text = f"{self.cached_atr:.2f}" if self.cached_atr is not None else "Calculando..."
        ema_text = f"{self.cached_ema:.2f}" if self.cached_ema is not None else "Calculando..."
        vol_text = f"{self.cached_median_vol:.2f}" if self.cached_median_vol is not None else "Calculando..."
        s += f"  <b>ATR(1h)</b>: <code>{atr_text}</code>\n"
        s += f"  <b>EMA({self.ema_period}, 1h)</b>: <code>{ema_text}</code>\n"
        s += f"  <b>MedianVol(1m, 60p, USDT)</b>: <code>{vol_text}</code>\n"
        
        s += "\n<b>Gestión de Riesgo</b>\n"
        pnl_diario = sum(t.get("pnl", 0) for t in self.daily_trade_stats)
        s += f"  <b>PnL Hoy (aprox)</b>: <code>{pnl_diario:.2f} USDT</code>\n"
        s += f"  <b>Límite Pérdida</b>: <code>{DAILY_LOSS_LIMIT_PCT}%</code>\n"
        
        return s

    def _pivots_text(self):
        if not self.daily_pivots:
            return "📐 Pivotes no calculados aún."
        
        s = "<b>📐 Pivotes Camarilla (Clásica)</b>\n\n"
        
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

    # --- INDICADORES (CON MEDIANA USDT) ---
    async def update_indicators(self):
        try:
            # ATR (1h)
            kl_1h = await self._get_klines(interval="1h", limit=50)
            if not kl_1h or len(kl_1h) <= self.atr_period:
                logging.warning(f"No hay suficientes klines para ATR (necesita {self.atr_period}, obtuvo {len(kl_1h)})")
            else:
                highs = [float(k[2]) for k in kl_1h]
                lows = [float(k[3]) for k in kl_1h]
                closes = [float(k[4]) for k in kl_1h]
                trs = []
                for i in range(1, len(kl_1h)):
                    tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                    trs.append(tr)
                if len(trs) >= self.atr_period:
                    first_atr = sum(trs[: self.atr_period]) / self.atr_period
                    atr = first_atr
                    alpha = 1.0 / self.atr_period
                    for tr in trs[self.atr_period :]:
                        atr = (tr * alpha) + (atr * (1 - alpha))
                    self.cached_atr = atr
                    logging.info("ATR(%d, 1h) actualizado: %s", self.atr_period, self.cached_atr)
            
            # EMA (1h)
            kl_ema = await self._get_klines(interval=self.ema_timeframe, limit=max(self.ema_period * 2, 100))
            if not kl_ema or len(kl_ema) <= self.ema_period:
                logging.warning(f"No hay suficientes klines para EMA (necesita {self.ema_period}, obtuvo {len(kl_ema)})")
            else:
                closes_ema = [float(k[4]) for k in kl_ema]
                if len(closes_ema) >= self.ema_period:
                    alpha = 2.0 / (self.ema_period + 1)
                    ema = closes_ema[0]
                    for price in closes_ema[1:]:
                        ema = (price * alpha) + (ema * (1 - alpha))
                    self.cached_ema = ema
                    logging.info("EMA(%d, 1h) actualizado: %s", self.ema_period, self.cached_ema)
            
            # --- MEDIANA de Volumen USDT (k[7]) ---
            kl_v = await self._get_klines(interval="1m", limit=61)
            if kl_v and len(kl_v) > 1:
                volumes = [float(k[7]) for k in kl_v[:-1]] 
                if volumes:
                    self.cached_median_vol = statistics.median(volumes) # Usar mediana
                    logging.info("MedianVol(1m, 60p, USDT) actualizado: %.2f", self.cached_median_vol)
        except Exception as e:
            logging.error("Error actualizando indicadores: %s", e)

    # --- CÁLCULO DE PIVOTES (CON DEBUG) ---
    @tenacity_retry_decorator_async()
    async def calculate_pivots(self):
        try:
            kl = await self._get_klines(interval="1d", limit=2)
            if len(kl) < 2:
                raise Exception("Insufficient daily klines")
            y = kl[-2]
            
            k_timestamp = datetime.utcfromtimestamp(y[0] / 1000).strftime('%Y-%m-%d')
            h, l, c = float(y[2]), float(y[3]), float(y[4])
            
            logging.info("-----------------------------------------------")
            logging.info(f"--- DEBUG DATOS DE PIVOTES (Vela de: {k_timestamp}) ---")
            logging.info(f"High (H): {h}")
            logging.info(f"Low (L): {l}")
            logging.info(f"Close (C): {c}")
            logging.info("-----------------------------------------------")

            if l == 0:
                raise Exception("Daily low zero")

            piv = (h + l + c) / 3.0
            rng = h - l
            r4 = c + (h - l) * 1.1 / 2
            r3 = c + (h - l) * 1.1 / 4
            r2 = c + (h - l) * 1.1 / 6
            r1 = c + (h - l) * 1.1 / 12
            s1 = c - (h - l) * 1.1 / 12
            s2 = c - (h - l) * 1.1 / 6
            s3 = c - (h - l) * 1.1 / 4
            s4 = c - (h - l) * 1.1 / 2
            r5 = (h / l) * c
            r6 = r5 + 1.168 * (r5 - r4)
            s5 = c - (r5 - c)
            s6 = c - (r6 - c)
            bc = (h + l) / 2.0
            tc = (piv - bc) + piv
            cw = abs(tc - bc) / piv * 100 if piv != 0 else 0

            lvls = {
                "P": piv, "BC": bc, "TC": tc, "width": cw, "is_ranging_day": cw > self.cpr_width_threshold,
                "H1": r1, "H2": r2, "H3": r3, "H4": r4, "H5": r5, "H6": r6,
                "L1": s1, "L2": s2, "L3": s3, "L4": s4, "L5": s5, "L6": s6,
            }
            
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
            await self._tg_send(self._pivots_text())

        except Exception as e:
            logging.error("Error calculating pivots: %s", e)
            if self.daily_pivots:
                logging.warning("Using previous pivots as fallback")
                await self._tg_send("⚠️ <b>ALERTA</b>\nFallo al calcular pivotes. Usando niveles previos.")
            else:
                await self._tg_send("🚨 <b>ERROR</b>\nFallo al calcular pivotes iniciales. Bot inactivo.")

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
    async def _get_median_volume_1m_usdt(self):
        if self.cached_median_vol:
            return self.cached_median_vol
        kl = await self._get_klines(interval="1m", limit=61)
        volumes = [float(k[7]) for k in kl[:-1]] 
        if volumes:
            median_vol = statistics.median(volumes)
            self.cached_median_vol = median_vol
            return median_vol
        return None

    # Place bracket order (con SL ID guardado y fix de deadlock)
    async def _place_bracket_order(self, side, qty, entry_price_signal, sl_price, tp_prices, entry_type):
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
        
        filled = False
        attempts = 0
        order_id = market.get("orderId")
        avg_price = 0.0
        executed_qty = 0.0
        
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

        sl_order_id = None

        try:
            batch = []
            num_tps = min(len(tp_prices), self.take_profit_levels)
            if num_tps == 0:
                raise Exception("No TP prices")
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))
            
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            if (side == SIDE_BUY and float(sl_price) >= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])) or \
                (side == SIDE_SELL and float(sl_price) <= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])):
                raise Exception("SL already surpassed by market price (fail-safe).")
            
            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "quantity": self._format_qty(executed_qty), "stopPrice": self._format_price(sl_price),
                "reduceOnly": "true"
            })
            
            remaining = Decimal(str(executed_qty))
            for i, tp in enumerate(tp_prices[:num_tps]):
                qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                qty_str = self._format_qty(qty_dec)
                
                if i == num_tps - 1 and remaining > 0 and remaining < Decimal(str(self.step_size)):
                    logging.warning("Cantidad restante de TP menor que step_size, omitiendo último TP.")
                    continue
                
                remaining -= Decimal(qty_str)
                mark_price = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
                tp_f = float(tp)
                
                if (side == SIDE_BUY and tp_f <= mark_price) or (side == SIDE_SELL and tp_f >= mark_price):
                    batch.append({
                        "symbol": self.symbol, "side": sl_side, "type": ORDER_TYPE_MARKET,
                        "quantity": qty_str, "reduceOnly": "true"
                    })
                else:
                    batch.append({
                        "symbol": self.symbol, "side": sl_side, "type": TAKE_PROFIT_MARKET,
                        "quantity": qty_str, "stopPrice": self._format_price(tp_f), "reduceOnly": "true"
                    })
            
            results = await self.client.futures_place_batch_order(batchOrders=batch)
            logging.info("SL/TP batch response: %s", results)
            
            if results and len(results) > 0 and "orderId" in results[0]:
                sl_order_id = results[0]["orderId"]
                logging.info(f"SL Order ID guardado: {sl_order_id}")
            else:
                logging.error("No se pudo obtener el orderId del SL del batch response.")

        except Exception as e:
            logging.error("Fallo creando SL/TP: %s", e)
            await self._tg_send(f"⚠️ <b>FAIL-SAFE</b>\nFallo SL/TP: {e}")
            await self._close_position_manual(reason="Fallo al crear SL/TP batch")
            return 

        self.is_in_position = True
        self.current_position_info = {
            "side": side,
            "quantity": executed_qty,
            "entry_price": avg_price,
            "entry_type": entry_type,
            "mark_price_entry": mark_price_entry,
            "atr_at_entry": self.cached_atr,
            "tps_hit_count": 0,
            "entry_time": time.time(),
            "sl_order_id": sl_order_id,
            "total_pnl": 0.0,
        }
        self.last_known_position_qty = executed_qty
        self.sl_moved_to_be = False
        self.trade_cooldown_until = time.time() + 300
        self.save_state()

        try:
            icon = "🔼" if side == SIDE_BUY else "🔽"
            tp_list_str = ", ".join([self._format_price(tp) for tp in tp_prices])
            
            msg = f"{icon} <b>NUEVA ORDEN: {entry_type}</b> {icon}\n\n"
            msg += f"<b>Símbolo</b>: <code>{self.symbol}</code>\n"
            msg += f"<b>Lado</b>: <code>{side}</code>\n"
            msg += f"<b>Cantidad</b>: <code>{self._format_qty(executed_qty)}</code>\n"
            msg += f"<b>Entrada</b>: <code>{self._format_price(avg_price)}</code>\n"
            msg += f"<b>SL</b>: <code>{self._format_price(sl_price)}</code> (ID: {sl_order_id})\n"
            msg += f"<b>TPs</b>: <code>{tp_list_str}</code>\n"
            atr_text = f"{self.cached_atr:.2f}" if self.cached_atr is not None else "N/A"
            msg += f"<b>ATR en Entrada</b>: <code>{atr_text}</code>\n"
            
            await self._tg_send(msg)
        except Exception as e:
            logging.error("Fallo enviando Telegram de nueva orden: %s", e)

    # --- Mover SL a BE (Inteligente, en TP2) ---
    async def _move_sl_to_be(self, remaining_qty_float):
        if self.sl_moved_to_be:
            return
        
        logging.info("Moviendo SL a Break-Even (disparado por TP2)...")
        try:
            entry_price = self.current_position_info.get("entry_price")
            side = self.current_position_info.get("side")
            old_sl_id = self.current_position_info.get("sl_order_id")
            
            if not entry_price or not side:
                logging.warning("No se puede mover SL a BE, falta info de entrada.")
                return

            if old_sl_id:
                try:
                    await self.client.futures_cancel_order(symbol=self.symbol, orderId=old_sl_id)
                    logging.info(f"Antiguo SL (ID: {old_sl_id}) cancelado.")
                except BinanceAPIException as e:
                    if e.code == -2011: 
                        logging.warning("SL antiguo ya no existía, continuando.")
                    else:
                        raise e
            else:
                logging.warning("No se encontró old_sl_id para cancelar.")

            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            new_sl_order = await self.client.futures_create_order(
                symbol=self.symbol,
                side=sl_side,
                type=STOP_MARKET,
                quantity=self._format_qty(remaining_qty_float),
                stopPrice=self._format_price(entry_price),
                reduceOnly="true"
            )
            
            new_sl_id = new_sl_order.get("orderId")
            
            self.sl_moved_to_be = True
            self.current_position_info["sl_order_id"] = new_sl_id
            self.save_state()
            
            await self._tg_send(f"🛡️ <b>TP2 ALCANZADO</b>\nSL movido a Break-Even: <code>{self._format_price(entry_price)}</code> (Nuevo SL ID: {new_sl_id})")

        except Exception as e:
            logging.error("Error moviendo SL a BE: %s", e)
            await self._tg_send("⚠️ Error al mover SL a Break-Even.")

    # --- Cierre Manual (para Time Stop y /cerrar) ---
    async def _close_position_manual(self, reason="Manual Close"):
        logging.warning(f"Cerrando posición manualmente: {reason}")
        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.symbol)
            
            pos = await self._get_current_position()
            qty = float(pos.get("positionAmt", 0))
            
            if qty == 0:
                logging.info("Intento de cierre manual, pero la posición ya es 0.")
                if self.is_in_position:
                    self.is_in_position = False
                    self.current_position_info = {}
                    self.last_known_position_qty = 0.0
                    self.sl_moved_to_be = False
                    self.save_state()
                return

            close_side = SIDE_SELL if qty > 0 else SIDE_BUY
            close_qty = abs(qty)

            await self.client.futures_create_order(
                symbol=self.symbol,
                side=close_side,
                type=ORDER_TYPE_MARKET,
                quantity=self._format_qty(close_qty),
                reduceOnly="true"
            )
            
            logging.info(f"Orden MARKET de cierre enviada. Razón: {reason}")
            
        except Exception as e:
            logging.error(f"Error en _close_position_manual: {e}")
            await self._tg_send(f"🚨 <b>ERROR</b>\nFallo al intentar cierre manual ({reason}).")


    # -------------- CORE STRATEGY (CON FILTRO DE VOLUMEN RE-ACTIVADO) --------------
    async def seek_new_trade(self, kline):
        if self.trading_paused:
            return
        
        now_ts = time.time()
        if now_ts < self.trade_cooldown_until:
            return
        if not self.daily_pivots:
            logging.debug("No pivots yet")
            return
        if self.cached_atr is None or self.cached_ema is None or self.cached_median_vol is None:
            logging.debug(f"Indicators not ready (ATR: {self.cached_atr is not None}, EMA: {self.cached_ema is not None}, Vol: {self.cached_median_vol is not None})")
            return
        
        # --- CAMBIO v65: Eliminar lock redundante ---
        async with self.lock:
            try:
                # --- CAMBIO v65: Usar la variable de estado, no llamar a la API ---
                if self.is_in_position:
                    return
                
                current_price = float(kline["c"])
                current_volume = float(kline["q"]) # Volumen USDT
                
                median_vol = self.cached_median_vol
                if not median_vol:
                    logging.debug("median vol (1m, USDT) missing")
                    return
                
                # --- v64: FILTRO DE VOLUMEN RE-ACTIVADO ---
                required_volume = median_vol * self.volume_factor
                volume_confirmed = current_volume > required_volume
                
                p = self.daily_pivots
                atr = self.cached_atr
                ema = self.cached_ema
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                # --- LÓGICA DE TRADING (con DEBUG) ---
                
                # breakout long
                if current_price > p["H4"]:
                    if volume_confirmed and current_price > ema:
                        side = SIDE_BUY
                        entry_type = "Breakout Long"
                        sl = current_price - atr * self.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.breakout_tp_mult]
                    else:
                        logging.info(f"[DEBUG H4] Rechazado. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Requerido: {required_volume:.0f}), EMA: {current_price > ema} (Precio: {current_price} > EMA: {ema:.2f})")
                
                # breakout short
                elif current_price < p["L4"]:
                    if volume_confirmed and current_price < ema:
                        side = SIDE_SELL
                        entry_type = "Breakout Short"
                        sl = current_price + atr * self.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.breakout_tp_mult]
                    else:
                        logging.info(f"[DEBUG L4] Rechazado. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Requerido: {required_volume:.0f}), EMA: {current_price < ema} (Precio: {current_price} < EMA: {ema:.2f})")
                
                # ranging long
                elif current_price <= p["L3"]:
                    if volume_confirmed:
                        side = SIDE_BUY
                        entry_type = "Ranging Long"
                        sl = p["L4"] - atr * self.ranging_atr_multiplier
                        tp_prices = [p["P"], p["H1"], p["H2"]]
                    else:
                        logging.info(f"[DEBUG L3] Rechazado. Precio OK (Precio: {current_price} <= L3: {p['L3']}). Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Requerido: {required_volume:.0f})")

                # ranging short
                elif current_price >= p["H3"]:
                    if volume_confirmed:
                        side = SIDE_SELL
                        entry_type = "Ranging Short"
                        sl = p["H4"] + atr * self.ranging_atr_multiplier
                        tp_prices = [p["P"], p["L1"], p["L2"]]
                    else:
                        logging.info(f"[DEBUG H3] Rechazado. Precio OK (Precio: {current_price} >= H3: {p['H3']}). Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Requerido: {required_volume:.0f})")
                
                # --- FIN LÓGICA ---

                if side:
                    balance = await self._get_account_balance()
                    if balance is None:
                        return
                    if await self._daily_loss_exceeded(balance):
                        await self._tg_send("❌ <b>Daily loss limit reached</b> — trading paused for the day.")
                        self.trade_cooldown_until = time.time() + 86400
                        return
                    
                    invest = balance * self.investment_pct
                    qty = float(self._format_qty((invest * self.leverage) / current_price))
                    if qty <= 0:
                        logging.warning("Qty computed 0; skip")
                        return
                    
                    if entry_type.startswith("Breakout"):
                        tp_prices = [current_price + (atr * self.breakout_tp_mult) if side == SIDE_BUY else current_price - (atr * self.breakout_tp_mult)]
                    
                    tp_prices = tp_prices[: self.take_profit_levels]
                    tp_prices_fmt = [float(self._format_price(tp)) for tp in tp_prices if tp is not None]
                    
                    logging.info("!!! SEÑAL !!! %s %s ; qty %s ; SL %s ; TPs %s", entry_type, side, qty, sl, tp_prices_fmt)
                    await self._place_bracket_order(side, qty, current_price, sl, tp_prices_fmt, entry_type)
            except Exception as e:
                logging.error(f"seek_new_trade error: {e}", exc_info=True)

    # -------------- DAILY LOSS CHECK --------------
    async def _daily_loss_exceeded(self, balance):
        total_pnl = self.current_position_info.get("total_pnl", 0)
        total_pnl += sum(t.get("pnl", 0) for t in self.daily_trade_stats)
        
        loss_limit = -abs((DAILY_LOSS_LIMIT_PCT / 100.0) * balance)
        return total_pnl <= loss_limit

    # -------------- KLINE WS HANDLER (1m candles) --------------
    async def handle_kline_evt(self, msg):
        if not msg:
            return
        if msg.get("e") == "error":
            logging.error("WS error event: %s", msg)
            return
        k = msg.get("k", {})
        if not k.get("x", False):
            return
        
        # --- CAMBIO v65: Solo llamar a seek_new_trade si NO estamos en posición ---
        # El account_poller se encarga de actualizar self.is_in_position
        if not self.is_in_position:
            await self.seek_new_trade(k)

    # -------------- ACCOUNT POLLER (CON GESTIÓN DE POSICIÓN) --------------
    async def account_poller_loop(self):
        logging.info("Account poller started (interval %.1fs)", self.account_poll_interval)
        while self.running:
            try:
                pos = await self._get_current_position()
                if not pos:
                    continue

                qty = abs(float(pos.get("positionAmt", 0)))
                
                if not self.is_in_position:
                    if qty > 0:
                        # RECONCILIACIÓN
                        logging.info("Detected open position by poll; syncing state")
                        self.is_in_position = True
                        self.current_position_info = {
                            "quantity": qty,
                            "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0,
                            "entry_time": time.time(),
                            "total_pnl": 0.0,
                        }
                        self.last_known_position_qty = qty
                        await self._tg_send("🔁 Posición detectada por poll; bot sincronizado.")
                        self.save_state()
                    continue 

                # --- LÓGICA 2: Estamos en posición (según el bot) ---
                
                if qty == 0:
                    # --- DETECCIÓN DE CIERRE TOTAL ---
                    logging.info("Posición cerrada detectada por poller.")
                    pnl = 0.0
                    close_px = 0.0
                    roi = 0.0
                    
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.symbol, limit=1))[0]
                        pnl = float(last_trade.get("realizedPnl", 0.0))
                        close_px = float(last_trade.get("price", 0.0))
                    except Exception as e:
                        logging.error("Error al obtener último trade para PnL: %s", e)

                    total_pnl = self.current_position_info.get("total_pnl", 0) + pnl
                    entry_price = self.current_position_info.get("entry_price", 0.0)
                    quantity = self.current_position_info.get("quantity", 0.0)
                    
                    if entry_price > 0 and quantity > 0 and self.leverage > 0:
                        initial_margin = (entry_price * quantity) / self.leverage
                        if initial_margin > 0:
                            roi = (total_pnl / initial_margin) * 100

                    td = {
                        "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_type": self.current_position_info.get("entry_type", "Unknown"),
                        "side": self.current_position_info.get("side", "Unknown"),
                        "quantity": quantity,
                        "entry_price": entry_price,
                        "mark_price_entry": self.current_position_info.get("mark_price_entry", 0.0),
                        "close_price_avg": close_px, 
                        "pnl": total_pnl, 
                        "pnl_percent_roi": roi, 
                        "cpr_width": self.daily_pivots.get("width", 0),
                        "atr_at_entry": self.current_position_info.get("atr_at_entry", 0),
                        "ema_filter": self.current_position_info.get("ema_at_entry", 0)
                    }
                    self._log_trade_to_csv(td)
                    self.daily_trade_stats.append({"pnl": total_pnl, "roi": roi})
                    
                    icon = "✅" if total_pnl >= 0 else "❌"
                    msg = (
                        f"{icon} <b>POSICIÓN CERRADA</b> {icon}\n\n"
                        f"<b>Tipo</b>: <code>{self.current_position_info.get('entry_type', 'N/A')}</code>\n"
                        f"<b>PnL Total</b>: <code>{total_pnl:+.2f} USDT</code>\n"
                        f"<b>ROI</b>: <code>{roi:+.2f}%</code> (sobre margen inicial)\n"
                    )
                    await self._tg_send(msg)
                    
                    self.is_in_position = False
                    self.current_position_info = {}
                    self.last_known_position_qty = 0.0
                    self.sl_moved_to_be = False
                    self.save_state()
                    continue 
                
                if qty < self.last_known_position_qty:
                    # --- DETECCIÓN DE TP PARCIAL ---
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.symbol, limit=1))[0]
                        partial_pnl = float(last_trade.get("realizedPnl", 0.0))
                    except Exception:
                        partial_pnl = 0.0
                    
                    tp_hit_count = self.current_position_info.get("tps_hit_count", 0) + 1
                    self.current_position_info["tps_hit_count"] = tp_hit_count
                    self.current_position_info["total_pnl"] = self.current_position_info.get("total_pnl", 0) + partial_pnl
                    
                    logging.info(f"TP PARCIAL ALCANZADO (TP{tp_hit_count}). Qty restante: {qty}. PnL: {partial_pnl}")
                    await self._tg_send(f"🎯 <b>TP{tp_hit_count} ALCANZADO</b>\nPnL: <code>{partial_pnl:+.2f}</code> | Qty restante: {qty}")
                    
                    self.last_known_position_qty = qty
                    self.save_state()
                    
                    if tp_hit_count == 2 and not self.sl_moved_to_be:
                        await self._move_sl_to_be(qty)
                
                # --- DETECCIÓN DE TIME STOP (6 HORAS) ---
                if (not self.sl_moved_to_be and 
                    self.current_position_info.get("entry_type", "").startswith("Ranging")):
                    
                    entry_time = self.current_position_info.get("entry_time", 0)
                    if entry_time > 0:
                        hours_in_trade = (time.time() - entry_time) / 3600
                        
                        if hours_in_trade > 6: # 6 Horas
                            logging.warning(f"TIME STOP (6h) triggered for Ranging trade. Closing position.")
                            await self._tg_send(f"⏳ <b>CIERRE POR TIEMPO</b>\nTrade de Rango (L3/H3) superó 6h. Cerrando posición.")
                            
                            await self._close_position_manual(reason="Time Stop 6h")
            
            except Exception as e:
                # No loguear -1003, es normal si el poller es rápido, pero sí loguear otros
                if "APIError(code=-1003)" not in str(e):
                    logging.error(f"Account poller loop error: {e}", exc_info=True)
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
        await asyncio.gather(self.calculate_pivots(), self.update_indicators())
        last_indicator_update = datetime.utcnow()
        while self.running:
            try:
                now = datetime.utcnow()
                if now.time() >= dt_time(0, 2) and (self.last_pivots_date is None or now.date() > self.last_pivots_date):
                    await self.calculate_pivots()
                if (now - last_indicator_update).total_seconds() >= self.indicator_update_interval_minutes * 60:
                    await self.update_indicators()
                    last_indicator_update = now
                
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
        logging.info("Iniciando bot asíncrono v65...")
        if TESTNET_MODE:
            logging.warning("¡¡¡ ATENCIÓN: v65 corriendo en MODO TESTNET !!!")
        
        self.client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET_MODE)
        self.bsm = BinanceSocketManager(self.client)
        await self._get_exchange_info()
        self.load_state()
        
        if not TESTNET_MODE:
            try:
                pos = await self._get_current_position()
                if pos and float(pos.get("positionAmt", 0)) != 0:
                    logging.warning("Reconciliación: posición activa encontrada, sincronizando.")
                    self.is_in_position = True
                    if not self.current_position_info:
                        self.current_position_info = {
                            "quantity": abs(float(pos["positionAmt"])),
                            "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0,
                            "entry_time": time.time(),
                            "total_pnl": 0.0,
                        }
                    self.last_known_position_qty = abs(float(pos["positionAmt"]))
                    
                    await self._tg_send("🤖 Bot reiniciado y reconciliado: posición activa encontrada.")
                    self.save_state()
                else:
                    logging.info("No active position on reconcile.")
            except Exception as e:
                logging.error("Error during reconcile: %s", e)
        else:
             logging.info("Modo Testnet: reconciliación de posiciones omitida.")


        self.running = True
        tasks = []
        tasks.append(asyncio.create_task(self.timed_tasks_loop()))
        tasks.append(asyncio.create_task(self.account_poller_loop()))
        tasks.append(asyncio.create_task(self.telegram_poll_loop()))

        logging.info("Connecting WS 1m...")
        stream_ctx = self.bsm.kline_socket(symbol=self.symbol.lower(), interval="1m")

        try:
            async with stream_ctx as ksocket:
                logging.info("WS conectado, escuchando 1m klines...")
                
                while self.running:
                    try:
                        msg = await ksocket.recv() 
                        if msg:
                            asyncio.create_task(self.handle_kline_evt(msg))
                    
                    except Exception as e:
                        logging.error(f"WS recv/handle error: {e}")
                        await self._tg_send("🚨 <b>WS ERROR INTERNO</b>\nReiniciando conexión.")
                        await asyncio.sleep(5)
                        break 

        except Exception as e:
            logging.critical(f"WS fatal connection error: {e}")
            await self._tg_send("🚨 <b>WS FATAL ERROR</b>\nRevisar logs.")
        
        finally:
            logging.warning("Saliendo del bucle WS. Iniciando apagado...")
            self.running = False
            for t in tasks:
                t.cancel()
           
    async def shutdown(self):
        logging.warning("Shutdown recibido. Guardando estado.")
        self.save_state()
        try:
            await self.httpx_client.aclose()
        except Exception:
            pass
        try:
            if self.client:
                await self.client.close_connection()
        except Exception:
            pass
        logging.info("Estado guardado atómicamente. Saliendo.")
        try:
            sys.exit(0)
        except SystemExit:
            os._exit(0)

# -------------- Entrypoint --------------
async def main():
    bot = AsyncTradingBotV65()
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
        logging.critical("Error fatal nivel superior: %s", e, exc_info=True)
