import logging
import pandas as pd
from datetime import datetime, timedelta
from .utils import format_price, format_qty, SIDE_BUY, SIDE_SELL

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.config = bot_controller 
        
        # Config V303.2: NY Momentum Sniper (One Shot)
        self.risk_per_trade = 0.015   # 1.5%
        self.max_leverage = 5.0      
        self.min_rr = 1.5             
        
        # Filtros de Sesión
        self.ny_session_start = 13
        self.ny_session_end = 20
        
        # --- FIX LETAL #2: LÍMITE DIARIO DURO ---
        self.current_day = None
        self.trades_today = 0
        self.max_daily_trades = 1 # SOLO 1 BALA

        # Cooldown de seguridad (por si acaso)
        self.next_allowed_trade_ts = 0

    async def seek_new_trade(self, _):
        row = self.state.current_row
        current_dt = datetime.fromtimestamp(self.state.current_timestamp)
        
        # 1. GESTIÓN DE LÍMITE DIARIO
        day = current_dt.date()
        if self.current_day != day:
            self.current_day = day
            self.trades_today = 0 # Nuevo día, recargamos la bala
            
        # Si ya gastamos la bala de hoy, adiós.
        if self.trades_today >= self.max_daily_trades:
            return

        # 2. FILTRO DE SESIÓN (NY Only)
        if not (self.ny_session_start <= current_dt.hour < self.ny_session_end):
            return

        # 3. COOLDOWN ABSOLUTO
        if self.state.current_timestamp < self.next_allowed_trade_ts:
            return

        current_price = row.close
        atr = self.state.cached_atr
        
        # Indicadores
        donchian_high = getattr(row, 'donchian_high', 0)
        donchian_low = getattr(row, 'donchian_low', 0)
        atr_ma = getattr(row, 'atr_ma', 0)
        
        if not atr or donchian_high == 0 or atr_ma == 0: return

        # 4. FILTROS DE CALIDAD (Expansión y Rango)
        if atr < (atr_ma * 1.05): return # Exigimos expansión de volatilidad
        
        range_pct = (row.high - row.low) / row.close
        if range_pct < 0.003: return # Evitamos velas doji/muertas

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG ---
            # Ruptura del máximo de 2 horas
            is_breakout_up = current_price > donchian_high
            
            # Fuerza de cierre (Evitar mechas largas arriba)
            body_strength = (row.close - row.low) / (row.high - row.low + 0.00001)
            strong_close = body_strength > 0.6
            
            if is_breakout_up and strong_close:
                # --- FIX LETAL #3: CÁLCULO DESDE LA RUPTURA ---
                # No calculamos desde el cierre (que puede ser eufórico),
                # sino desde el nivel de ruptura + un filtro.
                breakout_level = donchian_high
                
                # Entry teórico (para cálculo de TP/SL)
                # Asumimos que entramos "mal" (al cierre o apertura sig), pero
                # anclamos el SL a la estructura, no a nuestra mala entrada.
                ref_price = breakout_level
                
                # SL: 1 ATR desde la ruptura (más estable)
                stop_loss = ref_price - (atr * 1.0)
                
                # TP: 1.8 ATR desde la ruptura
                take_profit = ref_price + (atr * 1.8)
                
                # Entry Real: El precio actual (para mandar la orden)
                entry = current_price 
                
                # Validar si vale la pena entrar tan tarde
                # Si el precio ya corrió más de 0.5 ATR desde la ruptura, es tarde.
                if (entry - breakout_level) > (atr * 0.5):
                    return 

                risk = entry - stop_loss
                if risk > 0:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "NY Sniper Long")

            # --- SETUP SHORT ---
            is_breakout_down = current_price < donchian_low
            
            body_strength_short = (row.high - row.close) / (row.high - row.low + 0.00001)
            strong_close_short = body_strength_short > 0.6
            
            if is_breakout_down and strong_close_short:
                breakout_level = donchian_low
                ref_price = breakout_level
                
                stop_loss = ref_price + (atr * 1.0)
                take_profit = ref_price - (atr * 1.8)
                
                entry = current_price
                
                # Filtro Late Entry
                if (breakout_level - entry) > (atr * 0.5):
                    return

                risk = stop_loss - entry
                if risk > 0:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "NY Sniper Short")

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
                    
                    # QUEMAR LA BALA DEL DÍA
                    self.trades_today += 1
                    # Cooldown extra de 4 horas por si acaso
                    self.next_allowed_trade_ts = self.state.current_timestamp + 14400 
                    
                    logging.info(f"!!! SIGNAL V303.2 !!! {label} | Daily: {self.trades_today}/1")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Time Exit: 8 velas (2 horas)
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                entry_time = self.state.current_position_info.get('entry_time', 0)
                current_time = self.state.current_timestamp
                
                candles_open = (current_time - entry_time) / 900 
                
                if candles_open >= 8:
                     await self.orders_manager.close_position_manual("Time Exit (Stagnant)")
            except: pass