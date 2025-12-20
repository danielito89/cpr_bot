import logging
import pandas as pd
from datetime import datetime
from .utils import format_price, format_qty, SIDE_BUY, SIDE_SELL

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.config = bot_controller 
        
        # Config V302: NY Momentum Scalp
        self.risk_per_trade = 0.01   # 1% (Scalping requiere gestión estricta)
        self.max_leverage = 5.0      
        self.min_rr = 1.2            # Buscamos probabilidad, no home runs
        
        # Filtros de Sesión y Frecuencia
        self.ny_session_start = 13   # 13:00 UTC
        self.ny_session_end = 20     # 20:00 UTC
        self.daily_trades = 0
        self.last_day_traded = None

    async def seek_new_trade(self, _):
        row = self.state.current_row
        current_ts = pd.to_datetime(self.state.current_timestamp, unit='s')
        
        # 1. FILTRO DE SESIÓN (CRÍTICO)
        # Si no estamos en NY, no se opera. Punto.
        if not (self.ny_session_start <= current_ts.hour < self.ny_session_end):
            return

        # 2. LÍMITE DIARIO (ANTI-AMETRALLADORA)
        current_day = current_ts.date()
        if self.last_day_traded != current_day:
            self.daily_trades = 0
            self.last_day_traded = current_day
        
        if self.daily_trades >= 2: # Máximo 2 balas por día
            return

        current_price = row.close
        atr = self.state.cached_atr
        
        # Indicadores V302
        adx = getattr(row, 'adx', 0)
        donchian_high = getattr(row, 'donchian_high', 0)
        donchian_low = getattr(row, 'donchian_low', 0)
        
        if not atr or donchian_high == 0: return

        # 3. FILTRO DE RÉGIMEN (ADX)
        # Si ADX < 20, el mercado está muerto. No romperá con fuerza.
        if adx < 20: return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (Breakout) ---
            # El precio rompe el máximo de la última hora
            is_breakout_up = current_price > donchian_high
            
            # Confirmación de vela de fuerza (Close cerca del High)
            # Evita wicks largos superiores
            body_strength = (row.close - row.low) / (row.high - row.low + 0.00001)
            strong_close = body_strength > 0.6
            
            if is_breakout_up and strong_close:
                entry = current_price
                # SL ajustado: 0.8 ATR (Si falla, falla rápido)
                stop_loss = entry - (atr * 0.8)
                
                # TP Fijo: 1.2R (Scalping de libro)
                risk = entry - stop_loss
                take_profit = entry + (risk * 1.2)
                
                if risk > 0:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "NY Breakout Long")

            # --- SETUP SHORT (Breakdown) ---
            is_breakout_down = current_price < donchian_low
            
            # Confirmación (Close cerca del Low)
            body_strength_short = (row.high - row.close) / (row.high - row.low + 0.00001)
            strong_close_short = body_strength_short > 0.6
            
            if is_breakout_down and strong_close_short:
                entry = current_price
                stop_loss = entry + (atr * 0.8)
                
                risk = stop_loss - entry
                take_profit = entry - (risk * 1.2)
                
                if risk > 0:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "NY Breakout Short")

            # EJECUCIÓN
            if best_setup:
                side, entry, sl, tp, label = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                risk_amount = balance * self.risk_per_trade
                sl_distance = abs(entry - sl)
                raw_qty = risk_amount / sl_distance
                
                max_notional = balance * self.max_leverage
                if (raw_qty * entry) > max_notional: raw_qty = max_notional / entry
                
                qty = float(format_qty(self.config.step_size, raw_qty))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    
                    # Actualizar contadores
                    self.daily_trades += 1
                    
                    logging.info(f"!!! SIGNAL V302 !!! {label} (Daily: {self.daily_trades}/2)")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Gestión Activa V302: Time Stop Rápido
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                entry_time = self.state.current_position_info.get('entry_time', 0)
                current_time = self.state.current_timestamp
                
                # Si en 4 velas (1 hora) no tocó TP ni SL, CERRAR.
                # El momentum en 15m no dura más que eso.
                candles_open = (current_time - entry_time) / 900 # 900s = 15m
                
                if candles_open >= 4:
                     await self.orders_manager.close_position_manual("Time Exit (Momentum Lost)")

            except: pass