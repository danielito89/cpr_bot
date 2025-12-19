import logging
import pandas as pd
from .utils import format_price, format_qty, SIDE_BUY, SIDE_SELL

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.config = bot_controller 
        
        # Configuración S/D
        self.zone_validity_candles = 96 # Las zonas expiran tras 24h (96 velas de 15m)
        self.min_rr = 2.0

    def _cleanup_zones(self, current_ts, current_price):
        # Eliminar zonas viejas o rotas
        valid_zones = []
        for z in self.state.active_zones:
            # 1. Tiempo: Si es muy vieja, borrar
            age_candles = (current_ts - z['created_at']) / 900 # 15m = 900s
            if age_candles > self.zone_validity_candles: continue
            
            # 2. Ruptura: Si el precio cruza la zona, ya no sirve (mitigada/rota)
            if z['type'] == 'DEMAND' and current_price < z['bottom']: continue # Rota a la baja
            if z['type'] == 'SUPPLY' and current_price > z['top']: continue # Rota al alza
            
            valid_zones.append(z)
        self.state.active_zones = valid_zones

    def _create_zone_if_impulse(self, row):
        # Si la vela actual es IMPULSO, la ANTERIOR es la zona
        if row.is_impulse:
            # Si es vela verde grande -> Demand Zone es la vela roja/verde pequeña previa
            if row.close > row.open: 
                # Demand Zone
                # Simplificación: Usamos el Low y High de la vela anterior como zona
                # En código real, buscaríamos la última vela contraria, pero esto es buen proxy
                # Como no tenemos acceso directo a row-1 aqui facil, usaremos una logica de precio
                # Asumimos que la base del impulso es aprox Open de esta vela y Low de esta vela
                zone = {
                    'type': 'DEMAND',
                    'top': row.open,
                    'bottom': row.low, # O el low de la anterior si pudieramos
                    'created_at': self.state.current_timestamp,
                    'tested': False
                }
                self.state.active_zones.append(zone)
            
            # Si es vela roja grande -> Supply Zone
            elif row.close < row.open:
                zone = {
                    'type': 'SUPPLY',
                    'top': row.high,
                    'bottom': row.open,
                    'created_at': self.state.current_timestamp,
                    'tested': False
                }
                self.state.active_zones.append(zone)

    async def seek_new_trade(self, kline_placeholder):
        row = self.state.current_row
        current_price = row.close
        
        # 1. GESTIÓN DE ZONAS
        self._cleanup_zones(self.state.current_timestamp, current_price)
        self._create_zone_if_impulse(row)
        
        # 2. DEFINICIÓN DE TENDENCIA (STRUCTURE)
        # Necesitamos Swing High/Low actuales y previos
        # Los pre-calculamos en el backtester
        sh = row.last_swing_high
        sl = row.last_swing_low
        psh = row.prev_swing_high
        psl = row.prev_swing_low
        
        # Evitar errores con NaNs al principio
        if pd.isna(sh) or pd.isna(psh): return

        is_uptrend = (sh > psh) and (sl > psl)
        is_downtrend = (sh < psh) and (sl < psl)
        
        if not is_uptrend and not is_downtrend: return # Rango/Indefinido -> No operar
        
        # 3. BUSCAR ENTRADA EN PULLBACK
        # Chequeamos si el precio está DENTRO de una zona válida alineada con la tendencia
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            best_setup = None
            
            for z in self.state.active_zones:
                if z['tested']: continue 
                
                # Setup LONG
                if is_uptrend and z['type'] == 'DEMAND':
                    # Precio entra en zona (Pullback)
                    # Tolerancia: Tocar el 'top' de la zona
                    if z['bottom'] <= current_price <= (z['top'] * 1.001): 
                        # Validar R/R
                        # SL: Debajo de la zona
                        stop_loss = z['bottom'] * 0.998
                        # TP: El último Swing High (Liquidez expuesta)
                        take_profit = sh
                        
                        risk = current_price - stop_loss
                        reward = take_profit - current_price
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_BUY, current_price, stop_loss, take_profit, "Demand Pullback")
                            z['tested'] = True # Marcar como usada para no repetir en la misma vela

                # Setup SHORT
                elif is_downtrend and z['type'] == 'SUPPLY':
                    # Precio entra en zona
                    if (z['bottom'] * 0.999) <= current_price <= z['top']:
                        stop_loss = z['top'] * 1.002
                        take_profit = sl # Target: Ultimo Swing Low
                        
                        risk = stop_loss - current_price
                        reward = current_price - take_profit
                        
                        if risk > 0 and (reward / risk) >= self.min_rr:
                            best_setup = (SIDE_SELL, current_price, stop_loss, take_profit, "Supply Pullback")
                            z['tested'] = True

            # EJECUTAR
            if best_setup:
                side, entry, sl, tp, label = best_setup
                
                balance = await self.bot._get_account_balance()
                if not balance: return
                
                # Size fijo 5%
                invest = balance * 0.05
                notional = invest * self.config.leverage
                qty = float(format_qty(self.config.step_size, notional / entry))
                
                if qty > 0:
                    tps = [float(format_price(self.config.tick_size, tp))]
                    logging.info(f"!!! S/D SIGNAL !!! {label} | R/R: {(abs(tp-entry)/abs(entry-sl)):.2f}")
                    await self.orders_manager.place_bracket_order(side, qty, entry, sl, tps, label)