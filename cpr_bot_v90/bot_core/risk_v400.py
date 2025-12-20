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
        
        # Config V400: Trend Liquidity Scalper
        self.risk_per_trade = 0.02   # 2% (WR alto permite subir riesgo un poco)
        self.max_leverage = 5.0      
        self.min_rr = 1.0            # Scalping: 1:1 es aceptable si WR > 60%
        
        # Cooldown para no ametrallar el mismo dip
        self.last_trade_ts = 0
        self.cooldown_candles = 4    # 1 Hora de espera tras trade

    async def seek_new_trade(self, _):
        # 1. COOLDOWN
        if (self.state.current_timestamp - self.last_trade_ts) < (self.cooldown_candles * 900):
            return

        row = self.state.current_row
        current_price = row.close
        atr = self.state.cached_atr
        
        # Indicadores V400
        trend_fast = getattr(row, 'trend_fast', 0) # 1H EMA 50 equiv
        trend_slow = getattr(row, 'trend_slow', 0) # 1H EMA 200 equiv
        ema_local  = getattr(row, 'ema_local', 0)  # 15m EMA 50
        rsi = getattr(row, 'rsi', 50)
        vol = row.volume
        vol_ma = getattr(row, 'vol_ma', 0)
        
        if not atr or trend_fast == 0: return

        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            # --- SETUP LONG (Buy The Dip) ---
            # 1. TENDENCIA MACRO (1H) ALCISTA
            is_macro_uptrend = trend_fast > trend_slow
            
            # 2. CONDICIÓN DE SOBREVENTA / DIP
            # Precio cae por debajo de la EMA local O RSI < 35
            is_dip = (current_price < ema_local) or (rsi < 35)
            
            # 3. VOLUMEN SANO
            # Queremos que el volumen actual no sea explosivo (eso sería crash),
            # sino una corrección controlada. O bien, volumen de parada.
            # En V400 simplificado: Entramos en la vela VERDE de recuperación.
            is_recovery_candle = row.close > row.open
            
            # 4. RECLAIM (El gatillo)
            # El precio estaba "barato" y ahora cierra con fuerza.
            if is_macro_uptrend and is_dip and is_recovery_candle:
                
                # Check de calidad de la vela
                body = row.close - row.open
                if body < (atr * 0.1): return # Doji, no sirve
                
                entry = current_price
                
                # SL: Estructural (Mínimo de la vela de entrada - buffer)
                # Scalping requiere stops ajustados para tener buen RR con TPs cortos.
                stop_loss = row.low - (atr * 0.3)
                
                # TP: Cashflow rápido (0.8 ATR a 1.2 ATR)
                # Buscamos volver a la media o un saltito rápido.
                risk = entry - stop_loss
                take_profit = entry + (atr * 0.8) # TP Fijo por volatilidad
                
                # Validación RR mínima
                if risk > 0 and (take_profit - entry) / risk >= 0.8: # Permitimos 0.8R por el alto WR
                    best_setup = (SIDE_BUY, entry, stop_loss, take_profit, "V400 Dip Long")

            # --- SETUP SHORT (Sell The Rally) ---
            is_macro_downtrend = trend_fast < trend_slow
            is_rally = (current_price > ema_local) or (rsi > 65)
            is_rejection_candle = row.close < row.open
            
            if is_macro_downtrend and is_rally and is_rejection_candle:
                
                body = row.open - row.close
                if body < (atr * 0.1): return 
                
                entry = current_price
                stop_loss = row.high + (atr * 0.3)
                take_profit = entry - (atr * 0.8)
                
                risk = stop_loss - entry
                if risk > 0 and (entry - take_profit) / risk >= 0.8:
                    best_setup = (SIDE_SELL, entry, stop_loss, take_profit, "V400 Rally Short")

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
                    self.last_trade_ts = self.state.current_timestamp
                    
                    logging.info(f"!!! SIGNAL V400 !!! {label} | RSI: {rsi:.1f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)

    async def check_position_state(self):
        # Gestión Activa: Scalping puro
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
                
                # TIME EXIT: Si en 2 horas (8 velas) no paga, fuera.
                entry_time = self.state.current_position_info.get('entry_time', 0)
                candles_open = (self.state.current_timestamp - entry_time) / 900
                
                if candles_open >= 8 and pnl_r < 0.2:
                     await self.orders_manager.close_position_manual("Time Exit (Stalled)")
                     return

                # BE AGRESIVO: Scalping = proteger rápido.
                # Al 0.6R protegemos la entrada.
                if pnl_r > 0.6 and not self.state.sl_moved_to_be:
                    await self.orders_manager.move_sl_to_be(qty)

            except: pass