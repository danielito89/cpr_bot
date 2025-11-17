#!/usr/bin/env python3
# main_v81.py
# Versión: v81.1 (Gestión Dinámica + Fix Crítico Websocket Futures)

import os
import sys
import asyncio
import logging
import signal
from binance import AsyncClient, BinanceSocketManager

from bot_core.utils import setup_logging
from bot_core.symbol_strategy import SymbolStrategy
from telegram.handler import TelegramHandler

# Configuración Global
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "orchestrator_v81.log")

logger = setup_logging(LOG_FILE)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TESTNET_MODE = os.environ.get("TESTNET_MODE", "false").lower() in ("1", "true", "yes")

# PARES INICIALES
INITIAL_SYMBOLS = ["BTCUSDT", "ETHUSDT"] 

# Configuración Ganadora (Backtest 6 Meses)
DEFAULT_CONFIG = {
    "investment_pct": 0.05,      # 5% Riesgo
    "leverage": 30,              # 30x Riesgo
    "cpr_width_threshold": 0.2,
    "volume_factor": 1.3,        # Optimizado
    "take_profit_levels": 3,
    "atr_period": 14,
    "ranging_atr_multiplier": 0.5,
    "breakout_atr_sl_multiplier": 1.0,
    "breakout_tp_mult": 1.25,
    "range_tp_mult": 2.0,
    "ema_period": 20,            # Optimizado
    "ema_timeframe": "1h",
    "indicator_update_interval_minutes": 15,
    "DAILY_LOSS_LIMIT_PCT": 0.15 # 15% (Aumentado para evitar paro temprano)
}

if not API_KEY or not API_SECRET:
    logging.critical("Falta BINANCE_API_KEY/BINANCE_SECRET_KEY")
    sys.exit(1)

class BotOrchestrator:
    def __init__(self):
        self.client = None
        self.bsm = None
        self.telegram_handler = None
        self.strategies = {} # { 'BTCUSDT': SymbolStrategyInstance }
        self.tasks = {}      # { 'BTCUSDT': asyncio.Task }
        self.running = True
        self.DEFAULT_CONFIG = DEFAULT_CONFIG # Para acceso desde Handler

    async def start(self):
        logging.info(f"Iniciando Orquestador v81.1 (Dinámico + Fix WS)...")
        
        # 1. Inicializar Cliente Binance
        self.client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET_MODE)
        
        # --- FIX CRÍTICO: Forzar Websocket de FUTUROS ---
        self.bsm = BinanceSocketManager(self.client)
        
        if TESTNET_MODE:
            self.bsm.STREAM_URL = 'wss://stream.binancefuture.com/ws/'
            logging.warning("BSM configurado para TESTNET Futures")
        else:
            # URL de Mainnet Futures (¡Vital!)
            self.bsm.STREAM_URL = 'wss://fstream.binance.com/ws/'
            logging.info("BSM configurado para MAINNET Futures")
        # -----------------------------------------------
        
        self.telegram_handler = TelegramHandler(
            orchestrator=self,
            token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID
        )

        # Iniciar Telegram
        tg_task = asyncio.create_task(self.telegram_handler.start_polling())
        
        # Iniciar Pares Iniciales
        for sym in INITIAL_SYMBOLS:
            await self.add_pair(sym)

        await self.telegram_handler._send_message(f"🚀 <b>Orquestador v81.1 Iniciado</b>\nPares: {', '.join(self.strategies.keys())}")

        try:
            # Mantener vivo el orquestador esperando a Telegram
            await tg_task
        except asyncio.CancelledError:
            logging.info("Orquestador detenido.")
        finally:
            await self.shutdown()

    async def add_pair(self, symbol):
        """Inicia un nuevo bot para un par."""
        symbol = symbol.upper()
        if symbol in self.strategies:
            logging.warning(f"{symbol} ya está corriendo.")
            return False
        
        try:
            logging.info(f"Iniciando estrategia para {symbol}...")
            strategy = SymbolStrategy(
                symbol=symbol,
                config=DEFAULT_CONFIG,
                client=self.client,
                bsm=self.bsm,
                telegram_handler=self.telegram_handler
            )
            self.strategies[symbol] = strategy
            # Crear tarea y guardarla
            self.tasks[symbol] = asyncio.create_task(strategy.run())
            return True
        except Exception as e:
            logging.error(f"Error iniciando {symbol}: {e}")
            return False

    async def remove_pair(self, symbol):
        """Detiene y elimina un bot para liberar recursos."""
        symbol = symbol.upper()
        if symbol not in self.strategies:
            return False
        
        logging.info(f"Deteniendo estrategia para {symbol}...")
        strategy = self.strategies[symbol]
        
        # 1. Parar lógica interna del bot
        await strategy.stop()
        
        # 2. Cancelar la tarea de asyncio
        task = self.tasks.get(symbol)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self.tasks[symbol]
        
        # 3. Eliminar referencia (Python Garbage Collector liberará la RAM)
        del self.strategies[symbol]
        logging.info(f"{symbol} eliminado completamente.")
        return True

    async def pause_all(self, target_symbol=None):
        if target_symbol:
            bot = self.strategies.get(target_symbol)
            if bot: await bot.pause_trading()
        else:
            for bot in self.strategies.values():
                await bot.pause_trading()

    async def resume_all(self, target_symbol=None):
        if target_symbol:
            bot = self.strategies.get(target_symbol)
            if bot: await bot.resume_trading()
        else:
            for bot in self.strategies.values():
                await bot.resume_trading()

    async def shutdown(self):
        logging.warning("Apagando Orquestador...")
        self.running = False
        
        # Copia de las keys para iterar seguro
        symbols = list(self.strategies.keys())
        for sym in symbols:
            await self.remove_pair(sym)
        
        if self.telegram_handler: await self.telegram_handler.stop()
        if self.client: await self.client.close_connection()
        
        logging.info("Apagado completo.")

async def main():
    orchestrator = BotOrchestrator()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(orchestrator.shutdown()))
        except Exception: pass
    await orchestrator.start()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.critical(f"Error fatal en main: {e}", exc_info=True)
