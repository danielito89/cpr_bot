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
        
        # Config V303: NY Momentum Sniper
        self.risk_per_trade = 0.015   # 1.5%
        self.max_leverage = 5.0      
        self.min_rr = 1.5             # Exigimos recorrido
        
        # Filtros de Sesión
        self.ny_session_start = 13
        self.ny_session_end = 20
        
        # Cooldown REAL (Timestamp)
        self.next_allowed_trade_ts = 0

    async def seek_new_trade(self, _):
        row = self.state.current_row
        # Convertir timestamp a objeto datetime para check de hora
        current_dt = datetime.fromtimestamp(self.state.current_timestamp)
        
        # 1. FILTRO DE SESIÓN (NY Only)
        if not (self.ny_session_start <= current_dt.hour < self.ny_session_end):
            return

        # 2. COOLDOWN ABSOLUTO (V303 FIX)
        # Si estamos "castigados" por tiempo, no operamos.
        if self.state.current_timestamp < self.next_allowed_trade_ts:
            return

        current_price = row.close
        atr = self.state.cached_atr
        
        # Indicadores V303
        donchian_high = getattr(row, 'donchian_high', 0)
        donchian_low = getattr(row, 'donchian_low', 0)
        atr_ma = getattr(row, 'atr_ma', 0)
        
        if not atr or donchian_high == 0 or atr_ma == 0: return

        # 3. FILTRO DE EXPANSIÓN (V303 FIX)
        # Solo entramos si la volatilidad está por encima de su media.
        # Esto evita entrar en rangos muertos.
        if atr < (atr_ma * 1.05): return
        
        # 4. FILTRO DE VELA MUERTA (V303 FIX)
        # Si la vela actual es microscópica, no hay momentum real.
        range_pct = (row.high - row.low) / row.close
        if range_pct < 0.003: return 

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (Breakout Estructural 2H) ---
            is_breakout_up = current_price > donchian_high
            
            # Confirmación: Cierre fuerte
            # El cierre debe estar en el tercio superior de la vela
            strong_close = row.close > (row.low + (row.high - row.low) * 0.66)
            
            if is_breakout_up and strong_close:
                entry = current_price
                # SL: 1.0 ATR (Espacio justo)
                stop_loss = entry - (atr * 1.0)
                
                # TP ASIMÉTRICO (V303 FIX): 1.8R
                risk = entry - stop_loss
                take_profit = entry + (risk * 1.8)
                
                if risk > 0:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "NY Sniper Long")

            # --- SETUP SHORT (Breakdown Estructural 2H) ---
            is_breakout_down = current_price < donchian_low
            
            # Confirmación: Cierre fuerte abajo
            strong_close_short = row.close < (row.low + (row.high - row.low) * 0.33)
            
            if is_breakout_down and strong_close_short:
                entry = current_price
                stop_loss = entry + (atr * 1.0)
                
                risk = stop_loss - entry
                take_profit = entry - (risk * 1.8)
                
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
                    
                    # ACTIVAR COOLDOWN: 2 HORAS (7200 segundos)
                    self.next_allowed_trade_ts = self.state.current_timestamp + 7200
                    
                    logging.info(f"!!! SIGNAL V303 !!! {label} | ATR Exp: {atr > atr_ma}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Time Exit de Seguridad (si se estanca, salimos)
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                entry_time = self.state.current_position_info.get('entry_time', 0)
                current_time = self.state.current_timestamp
                
                # Si pasaron 8 velas (2 horas) y no tocó TP/SL -> FUERA
                # Momentum que no paga rápido, no sirve.
                candles_open = (current_time - entry_time) / 900 
                
                if candles_open >= 8:
                     await self.orders_manager.close_position_manual("Time Exit (Stagnant)")

            except: pass