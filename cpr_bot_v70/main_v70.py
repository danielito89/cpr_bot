#!/usr/bin/env python3
# main_v70.py
# Versión: v70.3 (Arquitectura Refactorizada Completa)
# El controlador principal solo inicializa y orquesta los managers.

import os
import sys
import asyncio
import logging
import signal
from datetime import datetime, time as dt_time

from binance import AsyncClient, BinanceSocketManager

# --- Módulos Principales ---
from bot_core.utils import setup_logging, tenacity_retry_decorator_async
from bot_core.pivots import calculate_pivots_from_data
from bot_core.indicators import calculate_atr, calculate_ema, calculate_median_volume
from bot_core.state import StateManager
from bot_core.orders import OrdersManager
from bot_core.risk import RiskManager
from bot_core.streams import StreamManager
from telegram.handler import TelegramHandler
# --- Fin Imports ---

# --- Configuración de Archivos ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_FILE = os.path.join(LOG_DIR, "trading_bot_v70.log")
STATE_FILE = os.path.join(BASE_DIR, "bot_state_v70.json")
CSV_FILE = os.path.join(DATA_DIR, "trades_log_v70.csv") # Pasado al RiskManager

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
        self.CSV_FILE = CSV_FILE # Pasar la ruta del CSV

        # --- Clientes y Handlers ---
        self.client = None
        self.bsm = None

        # --- Inicialización de Módulos ---
        self.state = StateManager(STATE_FILE)
        self.telegram_handler = TelegramHandler(
            bot_controller=self, 
            state_manager=self.state,
            token=TELEGRAM_BOT_TOKEN, 
            chat_id=TELEGRAM_CHAT_ID
        )
        # (El resto se inicializa en self.run())
        self.orders_manager = None
        self.risk_manager = None
        self.stream_manager = None

        # --- Reglas de Exchange ---
        self.tick_size = None
        self.step_size = None

        # --- Control ---
        self.lock = asyncio.Lock()
        self.running = True
        self.account_poll_interval = 5.0
        self.indicator_update_interval_minutes = 15

    # --- Lógica de Estado (Delega al StateManager) ---
    def save_state(self): self.state.save_state()
    def load_state(self): self.state.load_state()

    # --- Comandos de Telegram (Llamados por TelegramHandler) ---
    async def pause_trading(self):
        self.state.trading_paused = True
        self.save_state()
        logging.info("Trading pausado por comando de Telegram.")

    async def resume_trading(self):
        self.state.trading_paused = False
        self.save_state()
        logging.info("Trading reanudado por comando de Telegram.")

    async def close_position_manual(self, reason="Comando /cerrar de Telegram"):
        if self.orders_manager:
            await self.orders_manager.close_position_manual(reason)

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

    # --- Tareas de Fondo ---
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
        logging.info(f"Iniciando bot asíncrono v70 (Arquitectura Refactorizada)...")

        self.client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET_MODE)
        self.bsm = BinanceSocketManager(self.client)
        await self._get_exchange_info()

        # --- Inicializar Módulos que dependen del client ---
        self.orders_manager = OrdersManager(
            client=self.client,
            state=self.state,
            telegram_handler=self.telegram_handler,
            config=self # Pasa la config principal
        )
        self.risk_manager = RiskManager(bot_controller=self)
        self.stream_manager = StreamManager(
            bot_controller=self,
            risk_manager=self.risk_manager,
            telegram_handler=self.telegram_handler
        )

        self.load_state()

        # --- Lógica de Reconciliación ---
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

        # --- Iniciar Tareas de Fondo ---
        self.running = True
        tasks = await self.stream_manager.get_tasks() # Obtiene klines, UDS, poller, telegram
        tasks.append(asyncio.create_task(self.timed_tasks_loop())) # Añade el loop de indicadores/pivotes

        logging.info("Todos los módulos inicializados. Corriendo tareas principales...")
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logging.info("Tareas principales canceladas.")
        finally:
            logging.warning("Bucle principal finalizado. Iniciando apagado...")
            await self.shutdown()

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
