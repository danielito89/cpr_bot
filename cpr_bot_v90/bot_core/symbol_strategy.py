#!/usr/bin/env python3
# bot_core/symbol_strategy.py
# Versión: v99 (Barrendero con Auto-Reset de Estado)

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
    SIDE_BUY, SIDE_SELL, CSV_HEADER
)
from .pivots import calculate_pivots_from_data
from .indicators import calculate_atr, calculate_ema, calculate_median_volume, calculate_adx
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
        
        # --- Configuración ---
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
        self.indicator_update_interval_minutes = config.get("indicator_update_interval_minutes", 3)
        self.daily_loss_limit_pct = config.get("DAILY_LOSS_LIMIT_PCT", 5.0)
        
        # Nuevos parámetros
        self.min_volatility_atr_pct = config.get("MIN_VOLATILITY_ATR_PCT", 0.5)
        self.trailing_stop_trigger_atr = config.get("TRAILING_STOP_TRIGGER_ATR", 1.5)
        self.trailing_stop_distance_atr = config.get("TRAILING_STOP_DISTANCE_ATR", 1.0)
        
        # Clientes y Handlers
        self.client = client
        self.telegram_handler = telegram_handler
        
        # Archivos
        BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.STATE_FILE = os.path.join(BASE_DIR, "data", f"bot_state_{symbol}.json")
        self.CSV_FILE = os.path.join(BASE_DIR, "data", f"trades_log_{symbol}.csv")

        # Inicialización
        self.state = StateManager(self.STATE_FILE)
        self.orders_manager = None
        self.risk_manager = None
        self.tick_size = None
        self.step_size = None
        self.lock = asyncio.Lock()
        self.running = True
        self.tasks = []
        
        logging.info(f"[{self.symbol}] Estrategia v99 (Sweeper + AutoReset) inicializada.")

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

    async def force_reset_state(self):
        logging.warning(f"[{self.symbol}] FORZANDO RESET DE ESTADO.")
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.last_known_position_qty = 0.0
        self.state.sl_moved_to_be = False
        self.state.trade_cooldown_until = 0 
        self.save_state()
        logging.info(f"[{self.symbol}] Estado reseteado.")

    async def _setup_exchange_settings(self):
        logging.info(f"[{self.symbol}] Configurando Exchange: Cruzado, Leverage {self.leverage}x...")
        try:
            await self.client.futures_change_leverage(symbol=self.symbol, leverage=self.leverage)
        except BinanceAPIException as e:
            logging.warning(f"[{self.symbol}] No se pudo cambiar apalancamiento: {e}")
        try:
            await self.client.futures_change_margin_type(symbol=self.symbol, marginType='CROSSED')
        except BinanceAPIException as e:
            if e.code != -4046: logging.warning(f"[{self.symbol}] Aviso sobre margen: {e}")

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
            highs_1h = [float(k[2]) for k in kl_1h]
            lows_1h = [float(k[3]) for k in kl_1h]
            closes_1h = [float(k[4]) for k in kl_1h]
            
            self.state.cached_atr = calculate_atr(kl_1h, self.atr_period)
            self.state.cached_ema = calculate_ema(kl_ema, self.ema_period)
            self.state.cached_median_vol = calculate_median_volume(kl_1m)
            self.state.cached_adx = calculate_adx(highs_1h, lows_1h, closes_1h, period=14)
            
            logging.info(f"[{self.symbol}] Indicadores: ATR={self.state.cached_atr:.2f}, EMA={self.state.cached_ema:.2f}, VolMed={self.state.cached_median_vol:.0f}")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error actualizando indicadores: {e}")

    async def calculate_pivots(self):
        try:
            kl_1d = await self._get_klines(interval="1d", limit=2)
            if len(kl_1d) < 2: raise Exception("Insufficient daily klines")
            y = kl_1d[-2]
            h, l, c = float(y[2]), float(y[3]), float(y[4])
            self.state.daily_pivots = calculate_pivots_from_data(h, l, c, self.tick_size, self.cpr_width_threshold)
            if self.state.daily_pivots:
                self.state.last_pivots_date = datetime.utcnow().date()
                logging.info(f"[{self.symbol}] Pivotes actualizados")
                await self._send_pivots_alert()
            else: raise Exception("Cálculo de pivotes devolvió None")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error al calcular pivotes: {e}")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR ({self.symbol})</b>\nFallo al calcular pivotes.")

    async def _send_pivots_alert(self):
        p = self.state.daily_pivots
        if not p: return
        
        # Helper para formatear usando el tick_size real
        def fmt(val):
            return format_price(self.tick_size, val)

        s = f"📊 <b>Pivotes Camarilla ({self.symbol})</b>\n\n"
        s += f"H: <code>{fmt(p.get('Y_H', 0))}</code>\n"
        s += f"L: <code>{fmt(p.get('Y_L', 0))}</code>\n"
        s += f"C: <code>{fmt(p.get('Y_C', 0))}</code>\n\n"
        
        s += f"🔥 <b>R6:</b> <code>{fmt(p.get('H6', 0))}</code>\n"
        s += f"🔴 <b>R5:</b> <code>{fmt(p.get('H5', 0))}</code>\n"
        s += f"🔴 R4: <code>{fmt(p.get('H4', 0))}</code>\n"
        s += f"🔴 R3: <code>{fmt(p.get('H3', 0))}</code>\n"
        
        s += f"⚪ <b>P (Central):</b> <code>{fmt(p.get('P', 0))}</code>\n\n"
        
        s += f"🟢 S3: <code>{fmt(p.get('L3', 0))}</code>\n"
        s += f"🔵 S4: <code>{fmt(p.get('L4', 0))}</code>\n"
        s += f"🔵 <b>S5:</b> <code>{fmt(p.get('L5', 0))}</code>\n"
        s += f"🔵 <b>S6:</b> <code>{fmt(p.get('L6', 0))}</code>\n"
        
        cw = p.get("width", 0)
        day_type = "Rango" if p.get("is_ranging_day", True) else "Tendencia"
        s += f"\n📅 <b>{day_type}</b> (CPR {cw:.2f}%)"
        
        await self.telegram_handler._send_message(s)

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
    
    # --- NUEVO: ORDER SWEEPER + AUTO RESET (v99) ---
    async def reconcile_open_orders_loop(self):
        """Verifica órdenes basura y corrige estado zombie."""
        logging.info(f"[{self.symbol}] Sweeper iniciado (60s).")
        while self.running:
            try:
                pos = await self._get_current_position()
                qty = abs(float(pos.get("positionAmt", 0))) if pos else 0.0
                
                if qty < 0.0001:
                    # 1. Cancelar órdenes basura
                    open_orders = await self.client.futures_get_open_orders(symbol=self.symbol)
                    if open_orders:
                        logging.warning(f"[{self.symbol}] 🧹 ZOMBIE ORDERS DETECTADAS ({len(open_orders)}). Cancelando...")
                        await self.client.futures_cancel_all_open_orders(symbol=self.symbol)
                        await self.telegram_handler._send_message(f"🧹 <b>{self.symbol}</b>: Órdenes basura eliminadas.")
                    
                    # 2. AUTO-RESET de Estado (Fix Definitivo)
                    if self.state.is_in_position:
                        logging.warning(f"[{self.symbol}] 💀 ESTADO ZOMBIE DETECTADO (Pos=0, Memoria=1). Reseteando...")
                        self.state.is_in_position = False
                        self.state.current_position_info = {}
                        self.state.last_known_position_qty = 0.0
                        self.state.sl_moved_to_be = False
                        self.state.save_state()
                        await self.telegram_handler._send_message(f"✅ <b>{self.symbol}</b>: Estado Zombie corregido automáticamente.")

            except BinanceAPIException as e:
                if e.code != -1003: logging.error(f"[{self.symbol}] Sweeper error: {e}")
            except Exception as e:
                logging.error(f"[{self.symbol}] Sweeper loop error: {e}")
            
            await asyncio.sleep(60)
    # -----------------------------------------------------

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

    async def run(self):
        try:
            await self._get_exchange_info()
            await self._setup_exchange_settings()
            self.orders_manager = OrdersManager(self.client, self.state, self.telegram_handler, self)
            self.risk_manager = RiskManager(bot_controller=self)
            self.load_state()
            
            # Auto-Healing al Inicio
            try:
                pos = await self._get_current_position()
                qty = abs(float(pos.get("positionAmt", 0))) if pos else 0.0
                if self.state.is_in_position and qty < 0.0001:
                    logging.warning(f"[{self.symbol}] Zombie al inicio detectado. Limpiando.")
                    self.state.is_in_position = False
                    self.state.save_state()
            except: pass

            if self.state.daily_start_balance is None:
                 self.state.daily_start_balance = await self._get_account_balance()
            
            self.tasks = [
                asyncio.create_task(self.timed_tasks_loop()),
                asyncio.create_task(self.account_poller_loop()),
                asyncio.create_task(self.reconcile_open_orders_loop()), # Activado
            ]
            logging.info(f"[{self.symbol}] Tareas de fondo iniciadas.")
        except Exception as e:
            logging.critical(f"[{self.symbol}] Error fatal en 'run': {e}", exc_info=True)

    async def stop(self):
        self.running = False
        for task in getattr(self, 'tasks', []): task.cancel()
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