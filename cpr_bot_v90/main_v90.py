#!/usr/bin/env python3
# main_v90.py
# Versión: v90.7 (Fix Definitivo: Dual Websocket URL + BNB)

import os
import sys
import asyncio
import logging
import signal
from binance import AsyncClient, BinanceSocketManager

from bot_core.utils import setup_logging
from bot_core.symbol_strategy import SymbolStrategy
from tg_services.handler import TelegramHandler # Nota: Asegúrate que tu carpeta se llame tg_services

# Configuración Global
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "orchestrator_v90.log")

logger = setup_logging(LOG_FILE)

API_KEY = os.environ.get("BINANCE_API_KEY")
API_SECRET = os.environ.get("BINANCE_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TESTNET_MODE = os.environ.get("TESTNET_MODE", "false").lower() in ("1", "true", "yes")

# --- PARES INICIALES (BNB AGREGADO) ---
INITIAL_SYMBOLS = ["BTCUSDT", "ETHUSDT", "1000PEPEUSDT"]

# Configuración Ganadora + Nuevos Filtros
DEFAULT_CONFIG = {
    "investment_pct": 0.05,
    "leverage": 15,
    "cpr_width_threshold": 0.2,
    "volume_factor": 1.1,        # Punto Dulce (1.1)
    "take_profit_levels": 3,
    "atr_period": 14,
    "ranging_atr_multiplier": 0.5,
    "breakout_atr_sl_multiplier": 1.0,
    "breakout_tp_mult": 1.25,
    "range_tp_mult": 2.0,
    "ema_period": 20,
    "ema_timeframe": "1h",
    "indicator_update_interval_minutes": 3,
    "DAILY_LOSS_LIMIT_PCT": float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "15.0")),
    
    "MIN_VOLATILITY_ATR_PCT": 0.5,     
    "TRAILING_STOP_TRIGGER_ATR": 5.0, # Por defecto Sniper (BTC/BNB)
    "TRAILING_STOP_DISTANCE_ATR": 1.0  
}

# Configuración Específica (Overrides)
# --- CONFIGURACIÓN UNIFICADA GANADORA (v105) ---
SYMBOL_CONFIGS = {
    # BTC: Adaptativo (Base 1.1) + Sniper
    "BTCUSDT": {
        "volume_factor": 1.1,               # Base
        "strict_volume_factor": 15.0,       # Adaptativo
        "breakout_tp_mult": 1.25,           # Sniper (TP Fijo)
        "trailing_stop_trigger_atr": 5.0,   # Desactivado
        "trailing_stop_distance_atr": 1.0
    },
    # ETH: Adaptativo (Base 1.2) + Runner
    "ETHUSDT": {
        "volume_factor": 1.1,               # Base
        "strict_volume_factor": 20.0,       # Adaptativo
        "breakout_tp_mult": 1.25,           # Runner
        "trailing_stop_trigger_atr": 1.25,  # Activo
        "trailing_stop_distance_atr": 1.0
    },
    # PEPE: Adaptativo (Base 1.2) + Runner
    "1000PEPEUSDT": {
        "volume_factor": 1.1,               # Base
        "strict_volume_factor": 20.0,       # Adaptativo
        "breakout_tp_mult": 1.25,           # Runner
        "trailing_stop_trigger_atr": 1.25,  # Activo
        "trailing_stop_distance_atr": 1.0,
        "investment_pct": 0.05
    }
}

if not API_KEY or not API_SECRET:
    logging.critical("Falta BINANCE_API_KEY/BINANCE_SECRET_KEY")
    sys.exit(1)

class BotOrchestrator:
    def __init__(self):
        self.client = None
        self.bsm_user = None      # Para User Data (/ws/)
        self.bsm_multiplex = None # Para Klines (Root)
        self.telegram_handler = None
        self.strategies = {} 
        self.tasks = []
        self.multiplex_task = None
        self.user_stream_task = None
        self.running = True
        self.DEFAULT_CONFIG = DEFAULT_CONFIG

    async def start(self):
        logging.info(f"Iniciando Orquestador v90.7...")
        
        self.client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET_MODE)
        
        # --- FIX DUAL SOCKET ---
        self.bsm_user = BinanceSocketManager(self.client)
        self.bsm_multiplex = BinanceSocketManager(self.client)
        
        if TESTNET_MODE:
            self.bsm_user.STREAM_URL = 'wss://stream.binancefuture.com/ws/'
            self.bsm_multiplex.STREAM_URL = 'wss://stream.binancefuture.com/'
            logging.warning("BSM: TESTNET Futures")
        else:
            # URL para User Stream (requiere /ws/)
            self.bsm_user.STREAM_URL = 'wss://fstream.binance.com/ws/'
            # URL para Multiplex (NO debe tener /ws/ porque la lib agrega stream?...)
            self.bsm_multiplex.STREAM_URL = 'wss://fstream.binance.com/'
            logging.info("BSM: MAINNET Futures (Dual URL Configured)")

        self.telegram_handler = TelegramHandler(
            orchestrator=self,
            token=TELEGRAM_BOT_TOKEN,
            chat_id=TELEGRAM_CHAT_ID
        )
        self.tasks.append(asyncio.create_task(self.telegram_handler.start_polling()))

        for sym in INITIAL_SYMBOLS:
            await self.add_pair(sym, restart_streams=False)

        await self.restart_streams()

        await self.telegram_handler._send_message(f"🚀 <b>Orquestador v90.7 Iniciado</b>\nEscuchando: {', '.join(self.strategies.keys())}")

        try:
            while self.running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            logging.info("Orquestador detenido.")
        finally:
            await self.shutdown()

    async def add_pair(self, symbol, restart_streams=True):
        symbol = symbol.upper()
        if symbol in self.strategies: return False
        
        try:
            logging.info(f"Registrando {symbol}...")
            # Aplicar configuración específica si existe
            pair_config = self.DEFAULT_CONFIG.copy()
            if symbol in SYMBOL_CONFIGS:
                pair_config.update(SYMBOL_CONFIGS[symbol])
                logging.info(f"[{symbol}] Configuración específica aplicada.")

            strategy = SymbolStrategy(
                symbol=symbol,
                config=pair_config,
                client=self.client,
                telegram_handler=self.telegram_handler
            )
            await strategy.run()
            self.strategies[symbol] = strategy
            
            if restart_streams:
                logging.info(f"Reiniciando streams para incluir {symbol}...")
                await self.restart_streams()
            return True
        except Exception as e:
            logging.error(f"Error iniciando {symbol}: {e}")
            return False

    async def remove_pair(self, symbol):
        symbol = symbol.upper()
        if symbol not in self.strategies: return False
        logging.info(f"Deteniendo {symbol}...")
        await self.strategies[symbol].stop()
        del self.strategies[symbol]
        await self.restart_streams()
        return True

    async def restart_streams(self):
        if self.multiplex_task: self.multiplex_task.cancel()
        if self.user_stream_task: self.user_stream_task.cancel()
        await asyncio.sleep(0.5)
        
        self.user_stream_task = asyncio.create_task(self.run_central_user_stream())
        
        if self.strategies:
            streams = [f"{sym.lower()}@kline_1m" for sym in self.strategies.keys()]
            logging.info(f"Conectando Multiplex a: {streams}")
            self.multiplex_task = asyncio.create_task(self.run_central_multiplex(streams))

    async def run_central_multiplex(self, streams):
        while self.running:
            try:
                # Usamos bsm_multiplex (URL limpia)
                async with self.bsm_multiplex.multiplex_socket(streams) as ms:
                    logging.info("Multiplex Socket CONECTADO.")
                    while self.running:
                        res = await ms.recv()
                        if res and 'data' in res:
                            stream_name = res.get('stream')
                            data = res.get('data')
                            if stream_name and data:
                                symbol = stream_name.split('@')[0].upper()
                                k = data.get('k')
                                
                                # Diagnóstico de Latido (Descomentar si quieres verificar)
                                # if k.get('x'): logging.info(f"[{symbol}] 💓 Vela: {k['c']}")
                                
                                if symbol in self.strategies:
                                    await self.strategies[symbol].process_kline(k)

            except asyncio.CancelledError:
                logging.info("Multiplex cancelado.")
                break
            except Exception as e:
                logging.error(f"Error en Multiplex: {e}. Reconectando en 5s...")
                await asyncio.sleep(5)

    async def run_central_user_stream(self):
        while self.running:
            try:
                # Usamos bsm_user (URL con /ws/)
                async with self.bsm_user.futures_user_socket() as us:
                    logging.info("User Stream CONECTADO.")
                    while self.running:
                        msg = await us.recv()
                        if not msg: continue
                        if msg.get('e') == 'ORDER_TRADE_UPDATE':
                            order_data = msg.get('o', {})
                            symbol = order_data.get('s')
                            if symbol and symbol in self.strategies:
                                await self.strategies[symbol].process_user_data('ORDER_TRADE_UPDATE', order_data)
                                
            except asyncio.CancelledError:
                logging.info("User Stream cancelado.")
                break
            except Exception as e:
                logging.error(f"Error en User Stream: {e}. Reconectando en 5s...")
                await asyncio.sleep(5)

    async def pause_all(self, target_symbol=None):
        if target_symbol:
            if target_symbol in self.strategies:
                await self.strategies[target_symbol].pause_trading()
        else:
            for bot in self.strategies.values():
                await bot.pause_trading()

    async def resume_all(self, target_symbol=None):
        if target_symbol:
            if target_symbol in self.strategies:
                await self.strategies[target_symbol].resume_trading()
        else:
            for bot in self.strategies.values():
                await bot.resume_trading()

    async def shutdown(self):
        logging.warning("Apagando Orquestador v90...")
        self.running = False
        if self.multiplex_task: self.multiplex_task.cancel()
        if self.user_stream_task: self.user_stream_task.cancel()
        for strategy in self.strategies.values():
            await strategy.stop()
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