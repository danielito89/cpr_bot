#!/usr/bin/env python3
# bot_core/symbol_strategy.py
# Versión: v90.2 (Fix Final: run() es verdaderamente NO-BLOQUEANTE)

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
    tenacity_retry_decorator_async, 
    format_price, format_qty,
    SIDE_BUY, SIDE_SELL
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
        telegram_handler
    ):
        self.symbol = symbol
        
        # Desempaquetar Configuración
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
        
        self.client = client
        self.telegram_handler = telegram_handler
        
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.STATE_FILE = os.path.join(BASE_DIR, "data", f"bot_state_{symbol}.json")
        self.CSV_FILE = os.path.join(BASE_DIR, "data", f"trades_log_{symbol}.csv")

        self.state = StateManager(self.STATE_FILE)
        self.orders_manager = None
        self.risk_manager = None
        self.tick_size = None
        self.step_size = None
        self.lock = asyncio.Lock()
        self.running = True
        self.tasks = []
        
        logging.info(f"[{self.symbol}] Estrategia v90 inicializada.")

    def save_state(self): self.state.save_state()
    def load_state(self): self.state.load_state()

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

    async def _setup_exchange_settings(self):
        logging.info(f"[{self.symbol}] Configurando Exchange: Margen Cruzado, Leverage {self.leverage}x...")
        try:
            await self.client.futures_change_leverage(symbol=self.symbol, leverage=self.leverage)
        except BinanceAPIException as e:
            logging.warning(f"[{self.symbol}] No se pudo cambiar apalancamiento: {e}")

        try:
            await self.client.futures_change_margin_type(symbol=self.symbol, marginType='CROSSED')
        except BinanceAPIException as e:
            if e.code != -4046:
                logging.warning(f"[{self.symbol}] Aviso sobre margen: {e}")

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
        p = self.state.daily_pivots
        if not p: return
        s = f"📊 <b>Pivotes ({self.symbol})</b>\n"
        s += f"R4: {p.get('H4')} | S4: {p.get('L4')}\n"
        cw = p.get("width", 0)
        day_type = "Rango" if p.get("is_ranging_day", True) else "Breakout"
        s += f"📅 Tipo: <b>{day_type}</b> (CPR: {cw:.2f}%)"
        await self.telegram_handler._send_message(s)

    async def timed_tasks_loop(self):
        """Solo maneja actualizaciones periódicas (indicadores/pivotes)."""
        logging.info(f"[{self.symbol}] Timed tasks loop started")
        if self.state.daily_start_balance is None:
             self.state.daily_start_balance = await self._get_account_balance()
             self.state.start_of_day = datetime.utcnow().date()

        await asyncio.gather(self.calculate_pivots(), self.update_indicators())
        last_indicator_update = datetime.utcnow()
        
        while self.running:
            try:
                now = datetime.utcnow()
                if now.time() >= dt_time(0, 1) and now.date() > self.state.start_of_day:
                    self.state.daily_start_balance = await self._get_account_balance()
                    self.state.daily_trade_stats = []
                    self.state.start_of_day = now.date()
                    self.save_state()

                if now.time() >= dt_time(0, 2) and (self.state.last_pivots_date is None or now.date() > self.state.last_pivots_date):
                    await self.calculate_pivots()

                if (now - last_indicator_update).total_seconds() >= self.indicator_update_interval_minutes * 60:
                    await self.update_indicators()
                    last_indicator_update = now
                
                await asyncio.sleep(10)
            except Exception as e:
                logging.error(f"[{self.symbol}] Timed tasks error: {e}")
                await asyncio.sleep(10)
    
    async def process_kline(self, k):
        if not k.get("x", False): return
        if not self.state.is_in_position:
            await self.risk_manager.seek_new_trade(k)

    async def process_user_data(self, event_type, data):
        if event_type == 'ORDER_TRADE_UPDATE':
            await self.risk_manager.check_position_state()

    async def account_poller_loop(self):
        logging.info(f"[{self.symbol}] Poller iniciado.")
        while self.running:
            await self.risk_manager.check_position_state()
            await asyncio.sleep(5.0) 

    # --- Run ---
    async def run(self):
        """Inicializa la estrategia y corre sus tareas internas (timers + poller)."""
        try:
            await self._get_exchange_info()
            await self._setup_exchange_settings()

            self.orders_manager = OrdersManager(self.client, self.state, self.telegram_handler, self)
            self.risk_manager = RiskManager(bot_controller=self)
            
            self.load_state()
            
            if self.state.daily_start_balance is None:
                 self.state.daily_start_balance = await self._get_account_balance()
            
            # --- FIX v90.2: Lanzar tareas SIN bloquear con await ---
            self.tasks = [
                asyncio.create_task(self.timed_tasks_loop()),
                asyncio.create_task(self.account_poller_loop()),
            ]
            logging.info(f"[{self.symbol}] Tareas de fondo iniciadas.")
            
            # NO hacemos await gather(*self.tasks) aquí.
            # Dejamos que corran en el loop principal del orquestador.
        
        except Exception as e:
            logging.critical(f"[{self.symbol}] Error fatal en 'run': {e}", exc_info=True)
            # No re-lanzamos para no matar al orquestador

    async def stop(self):
        """Detiene las tareas de este símbolo."""
        self.running = False
        for task in getattr(self, 'tasks', []):
            task.cancel()
        logging.info(f"[{self.symbol}] Tareas detenidas.")
    
    def _log_trade_to_csv(self, trade_data, csv_file_path):
        file_exists = os.path.isfile(csv_file_path)
        try:
            import csv
            with open(csv_file_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if not file_exists: writer.writeheader()
                writer.writerow(trade_data)
        except Exception as e:
            logging.error(f"Error al guardar CSV: {e}")
