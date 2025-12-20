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
        
        # Config V401: VWAP Reclaimer
        self.risk_per_trade = 0.02   # 2% (Calidad sobre cantidad)
        self.max_leverage = 5.0      
        self.min_rr = 1.0            # RR 1:1 es aceptable con alto WR en scalping
        
        # Filtros
        self.cooldown_ts = 0
        self.cooldown_minutes = 60   # 1 hora de espera tras trade

    async def seek_new_trade(self, _):
        # 1. COOLDOWN
        if self.state.current_timestamp < self.cooldown_ts:
            return

        row = self.state.current_row
        prev_row = self.state.prev_row
        if prev_row is None: return

        current_price = row.close
        atr = self.state.cached_atr
        
        # Indicadores V401
        ema_200 = getattr(row, 'ema_200', 0)
        vwap = getattr(row, 'vwap', 0)
        struct_low = getattr(row, 'struct_low', 0)
        struct_high = getattr(row, 'struct_high', 0)
        
        if not atr or ema_200 == 0 or vwap == 0: return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (VWAP RECLAIM) ---
            # 1. Tendencia: Precio por encima de EMA 200 (Contexto Bullish)
            is_uptrend = current_price > ema_200
            
            # 2. El Evento: La vela ANTERIOR estaba por debajo del VWAP (Dip)
            # Ojo: Usamos prev_row.close o prev_row.low para confirmar que estuvimos abajo
            was_below = prev_row.close < vwap
            
            # 3. El Gatillo (Reclaim): La vela ACTUAL cierra por ENCIMA del VWAP
            is_reclaim = (row.close > vwap) and (row.close > row.open)
            
            if is_uptrend and was_below and is_reclaim:
                entry = current_price
                
                # SL ESTRUCTURAL (CRÍTICO)
                # Ponemos el SL en el mínimo reciente (struct_low)
                # Si el struct_low está muy cerca (< 0.5 ATR), le damos aire.
                structural_sl = struct_low
                min_sl_dist = atr * 0.8 # Mínimo espacio vital
                
                if (entry - structural_sl) < min_sl_dist:
                    stop_loss = entry - min_sl_dist
                else:
                    stop_loss = structural_sl - (atr * 0.1) # Pequeño buffer
                
                # TP: Cashflow (1.2 ATR)
                # No buscamos la luna, buscamos liquidez rápida.
                risk = entry - stop_loss
                take_profit = entry + (atr * 1.2)
                
                # Validación RR (Flexible para scalping)
                if risk > 0 and (take_profit - entry) / risk >= 0.9:
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "VWAP Reclaim Long")

            # --- SETUP SHORT (VWAP LOST) ---
            is_downtrend = current_price < ema_200
            was_above = prev_row.close > vwap
            is_lost = (row.close < vwap) and (row.close < row.open)
            
            if is_downtrend and was_above and is_lost:
                entry = current_price
                
                structural_sl = struct_high
                min_sl_dist = atr * 0.8
                
                if (structural_sl - entry) < min_sl_dist:
                    stop_loss = entry + min_sl_dist
                else:
                    stop_loss = structural_sl + (atr * 0.1)
                
                risk = stop_loss - entry
                take_profit = entry - (atr * 1.2)
                
                if risk > 0 and (entry - take_profit) / risk >= 0.9:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "VWAP Lost Short")

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
                    # Activar Cooldown
                    self.cooldown_ts = self.state.current_timestamp + (self.cooldown_minutes * 60)
                    
                    logging.info(f"!!! SIGNAL V401 !!! {label} | SL Struct: {sl:.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Gestión Activa: Protección Rápida
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                
                entry = float(pos['entryPrice'])
                mark = float(pos['markPrice'])
                sl = self.state.current_position_info.get('sl')
                side = self.state.current_position_info.get('side')
                qty = abs(float(pos['positionAmt']))
                
                if not sl: return
                risk_dist = abs(entry - sl)
                
                if side == SIDE_BUY: pnl_r = (mark - entry) / risk_dist
                else: pnl_r = (entry - mark) / risk_dist
                
                # BE al 0.8R (Asegurar fees y un poco más)
                if pnl_r > 0.8 and not self.state.sl_moved_to_be:
                    await self.orders_manager.move_sl_to_be(qty)
            except: pass