#!/usr/bin/env python3
# bot_core/symbol_strategy.py
# Versión: v82 (Incluye setup automático de apalancamiento y margen)

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

from binance.exceptions import BinanceAPIException

from .utils import (
    setup_logging, tenacity_retry_decorator_async, 
    format_price, format_qty, CSV_HEADER,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)
from .pivots import calculate_pivots_from_data
from .indicators import calculate_atr, calculate_ema, calculate_median_volume
from .state import StateManager
from .orders import OrdersManager
from .risk import RiskManager

class SymbolStrategy:
    def __init__(
        self,
        symbol,
        config,
        client,
        bsm,
        telegram_handler
    ):
        self.symbol = symbol
        
        # --- Desempaquetar Configuración ---
        self.investment_pct = config.get("investment_pct", 0.01)
        self.leverage = config.get("leverage", 3)
        self.cpr_width_threshold = config.get("cpr_width_threshold", 0.2)
        self.volume_factor = config.get("volume_factor", 1.3)
        self.take_profit_levels = config.get("take_profit_levels", 3)
        self.atr_period = config.get("atr_period", 14)
        self.ranging_atr_multiplier = config.get("ranging_atr_multiplier", 0.5)
        self.breakout_atr_sl_multiplier = config.get("breakout_atr_sl_multiplier", 1.0)
        self.breakout_tp_mult = config.get("breakout_tp_mult", 1.25)
        self.range_tp_mult = config.get("range_tp_mult", 2.0)
        self.ema_period = config.get("ema_period", 20)
        self.ema_timeframe = config.get("ema_timeframe", "1h")
        self.indicator_update_interval_minutes = config.get("indicator_update_interval_minutes", 15)
        self.daily_loss_limit_pct = config.get("DAILY_LOSS_LIMIT_PCT", 5.0)
        
        # --- Clientes y Handlers ---
        self.client = client
        self.bsm = bsm
        self.telegram_handler = telegram_handler
        
        # --- Archivos ---
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.STATE_FILE = os.path.join(BASE_DIR, "data", f"bot_state_{symbol}.json")
        self.CSV_FILE = os.path.join(BASE_DIR, "data", f"trades_log_{symbol}.csv")

        # --- Inicialización ---
        self.state = StateManager(self.STATE_FILE)
        self.orders_manager = None
        self.risk_manager = None
        self.tick_size = None
        self.step_size = None
        self.lock = asyncio.Lock()
        self.running = True
        self.tasks = []
        
        logging.info(f"[{self.symbol}] Estrategia inicializada.")

    # --- Lógica de Estado ---
    def save_state(self): self.state.save_state()
    def load_state(self): self.state.load_state()

    # --- Comandos ---
    async def pause_trading(self):
        self.state.trading_paused = True
        self.save_state()
        logging.info(f"[{self.symbol}] Trading pausado.")

    async def resume_trading(self):
        self.state.trading_paused = False
        self.save_state()
        logging.info(f"[{self.symbol}] Trading reanudado.")
    
    async def close_position_manual(self, reason="Cierre manual"):
        if self.orders_manager:
            await self.orders_manager.close_position_manual(reason)

    # --- Configuración Inicial de Binance (v82) ---
    async def _setup_exchange_settings(self):
        """Fuerza el apalancamiento y el tipo de margen en Binance."""
        logging.info(f"[{self.symbol}] Configurando Exchange: Margen Cruzado, Leverage {self.leverage}x...")
        
        # 1. Configurar Apalancamiento
        try:
            await self.client.futures_change_leverage(symbol=self.symbol, leverage=self.leverage)
            logging.info(f"[{self.symbol}] Apalancamiento fijado a {self.leverage}x")
        except BinanceAPIException as e:
            logging.warning(f"[{self.symbol}] No se pudo cambiar apalancamiento: {e}")

        # 2. Configurar Tipo de Margen (CROSSED o ISOLATED)
        try:
            await self.client.futures_change_margin_type(symbol=self.symbol, marginType='CROSSED')
            logging.info(f"[{self.symbol}] Margen fijado a CROSSED")
        except BinanceAPIException as e:
            # El error -4046 significa "No se necesita cambio" (ya está así), lo ignoramos.
            if e.code != -4046:
                logging.warning(f"[{self.symbol}] Aviso sobre margen: {e}")

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
                logging.info(f"[{self.symbol}] Reglas: Tick {self.tick_size}, Step {self.step_size}")
                return
        raise Exception(f"Símbolo {self.symbol} no encontrado en exchange info")

    # --- Indicadores ---
    async def update_indicators(self):
        try:
            kl_1h = await self._get_klines(interval="1h", limit=50)
            kl_ema = await self._get_klines(interval=self.ema_timeframe, limit=max(self.ema_period * 2, 100))
            kl_1m = await self._get_klines(interval="1m", limit=61)
            
            self.state.cached_atr = calculate_atr(kl_1h, self.atr_period)
            self.state.cached_ema = calculate_ema(kl_ema, self.ema_period)
            self.state.cached_median_vol = calculate_median_volume(kl_1m)
            
            logging.info(f"[{self.symbol}] Indicadores: ATR={self.state.cached_atr:.2f}, EMA={self.state.cached_ema:.2f}, VolMed={self.state.cached_median_vol:.0f}")
            
        except Exception as e:
            logging.error(f"[{self.symbol}] Error actualizando indicadores: {e}")

    # --- Pivotes ---
    async def calculate_pivots(self):
        try:
            kl_1d = await self._get_klines(interval="1d", limit=2)
            if len(kl_1d) < 2: raise Exception("Insufficient daily klines")
            
            y = kl_1d[-2]
            h, l, c = float(y[2]), float(y[3]), float(y[4])
            
            self.state.daily_pivots = calculate_pivots_from_data(
                h, l, c, self.tick_size, self.cpr_width_threshold
            )
            
            if self.state.daily_pivots:
                self.state.last_pivots_date = datetime.utcnow().date()
                logging.info(f"[{self.symbol}] Pivotes actualizados")
                await self._send_pivots_alert()
            else:
                raise Exception("Cálculo de pivotes devolvió None")

        except Exception as e:
            logging.error(f"[{self.symbol}] Error al calcular pivotes: {e}")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR ({self.symbol})</b>\nFallo al calcular pivotes.")

    async def _send_pivots_alert(self):
        """Genera y envía el mensaje de pivotes para este símbolo."""
        p = self.state.daily_pivots
        if not p: return

        s = f"📊 <b>Pivotes Camarilla ({self.symbol})</b>\n\n"
        s += f"H: <code>{p.get('Y_H', 0.0):.1f}</code>\n"
        s += f"L: <code>{p.get('Y_L', 0.0):.1f}</code>\n"
        s += f"C: <code>{p.get('Y_C', 0.0):.1f}</code>\n\n"
        
        s += f"🔥 <b>R6</b>: <code>{p.get('H6', 0.0):.2f}</code>\n"
        s += f"🔴 <b>R5</b>: <code>{p.get('H5', 0.0):.2f}</code>\n"
        s += f"🔴 R4: <code>{p.get('H4', 0.0):.2f}</code>\n"
        s += f"🔴 R3: <code>{p.get('H3', 0.0):.2f}</code>\n"
        s += f"🟡 R2: <code>{p.get('H2', 0.0):.2f}</code>\n"
        s += f"🟡 R1: <code>{p.get('H1', 0.0):.2f}</code>\n\n"
        
        s += f"🟢 S1: <code>{p.get('L1', 0.0):.2f}</code>\n"
        s += f"🟢 S2: <code>{p.get('L2', 0.0):.2f}</code>\n"
        s += f"🟢 S3: <code>{p.get('L3', 0.0):.2f}</code>\n"
        s += f"🔵 S4: <code>{p.get('L4', 0.0):.2f}</code>\n"
        s += f"🔵 S5: <code>{p.get('L5', 0.0):.2f}</code>\n"
        s += f"🔵 <b>S6</b>: <code>{p.get('L6', 0.0):.2f}</code>\n"
        
        cw = p.get("width", 0)
        is_ranging = p.get("is_ranging_day", True)
        day_type = "Rango" if is_ranging else "Breakout"
        s += f"\n📅 Tipo: <b>{day_type}</b> (CPR: {cw:.2f}%)"

        await self.telegram_handler._send_message(s)

    # --- Tareas de Fondo ---
    async def timed_tasks_loop(self):
        logging.info(f"[{self.symbol}] Timed tasks loop started")
        if self.state.daily_start_balance is None:
             self.state.daily_start_balance = await self._get_account_balance()
             self.state.start_of_day = datetime.utcnow().date()

        await asyncio.gather(self.calculate_pivots(), self.update_indicators())
        last_indicator_update = datetime.utcnow()
        
        while self.running:
            try:
                now = datetime.utcnow()
                
                if now.time() >= dt_time(0, 2) and (self.state.last_pivots_date is None or now.date() > self.state.last_pivots_date):
                    await self.calculate_pivots()

                if (now - last_indicator_update).total_seconds() >= self.config['indicator_update_interval_minutes'] * 60:
                    await self.update_indicators()
                    last_indicator_update = now
                
                await asyncio.sleep(10)
            except Exception as e:
                logging.error(f"[{self.symbol}] Timed tasks error: {e}")
                await asyncio.sleep(10)
    
    async def run_kline_loop(self):
        logging.info(f"[{self.symbol}] Connecting WS (Klines) 1m...")
        stream_ctx = self.bsm.kline_socket(symbol=self.symbol.lower(), interval="1m")
        
        while self.running:
            try:
                async with stream_ctx as ksocket:
                    logging.info(f"[{self.symbol}] WS (Klines) conectado.")
                    while self.running:
                        msg = await ksocket.recv() 
                        if msg: await self._handle_kline_evt(msg)
            except Exception as e:
                logging.error(f"[{self.symbol}] WS Error: {e}")
                await self.telegram_handler._send_message(f"🚨 <b>WS KLINE ERROR ({self.symbol})</b>")
                await asyncio.sleep(5)

    async def _handle_kline_evt(self, msg):
        if not msg or msg.get("e") == "error": return
        k = msg.get("k", {})
        if not k.get("x", False): return
        if not self.state.is_in_position:
            await self.risk_manager.seek_new_trade(k)

    # --- Run ---
    async def run(self):
        try:
            await self._get_exchange_info()
            
            # --- NUEVO v82: Forzar configuración de cuenta ---
            await self._setup_exchange_settings()
            # ---------------------------------------------

            self.orders_manager = OrdersManager(
                client=self.client,
                state=self.state,
                telegram_handler=self.telegram_handler,
                config=self 
            )
            self.risk_manager = RiskManager(bot_controller=self)
            self.load_state()
            
            # Reconciliación (Simplificada)
            try:
                pos = await self._get_current_position()
                if pos and float(pos.get("positionAmt", 0)) != 0:
                    logging.warning(f"[{self.symbol}] Reconciliación: posición activa.")
                    self.state.is_in_position = True
                    if not self.state.current_position_info:
                        self.state.current_position_info = {
                            "quantity": abs(float(pos["positionAmt"])),
                            "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0, "entry_time": time.time(), "total_pnl": 0.0,
                        }
                    self.state.last_known_position_qty = abs(float(pos["positionAmt"]))
                    self.state.save_state()
            except Exception: pass
            
            self.tasks = [
                asyncio.create_task(self.timed_tasks_loop()),
                asyncio.create_task(self.run_kline_loop()),
            ]
            await asyncio.gather(*self.tasks)
        
        except asyncio.CancelledError:
            logging.info(f"[{self.symbol}] Tareas canceladas.")
        except Exception as e:
            logging.critical(f"[{self.symbol}] Error fatal: {e}", exc_info=True)
        finally:
            self.running = False

    async def stop(self):
        self.running = False
        for task in getattr(self, 'tasks', []): task.cancel()
        logging.info(f"[{self.symbol}] Tareas detenidas.")
