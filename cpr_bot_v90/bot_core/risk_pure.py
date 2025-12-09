import logging
import time
from datetime import datetime
    
from .utils import (
    format_price, format_qty, 
    SIDE_BUY, SIDE_SELL
)

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.telegram_handler = bot_controller.telegram_handler
        self.config = bot_controller 

        self.max_trade_size_usdt = getattr(self.config, 'MAX_TRADE_SIZE_USDT', 50000)
        self.max_daily_trades = getattr(self.config, 'MAX_DAILY_TRADES', 50) 
        self.min_balance_buffer = 10 

    def _get_now(self):
        if hasattr(self.bot, 'get_current_timestamp'):
            return self.bot.get_current_timestamp()
        return time.time()

    async def can_trade(self, side, current_price):
        if self.state.trading_paused: return False, "Pausado"
        if self.state.is_in_position: return False, "Ya en posición"
        
        now = self._get_now()
        if now < self.state.trade_cooldown_until:
            wait = int(self.state.trade_cooldown_until - now)
            return False, f"Cooldown ({wait}s)"

        balance = await self.bot._get_account_balance()
        if balance is None: return False, "Error Balance"
        if balance < self.min_balance_buffer: return False, "Saldo Insuficiente"

        # Límite de Pérdida Diaria (Safety Net)
        start_bal = self.state.daily_start_balance if self.state.daily_start_balance else balance
        realized_pnl = sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        
        if start_bal > 0:
            daily_pnl_pct = (realized_pnl / start_bal) * 100
            if daily_pnl_pct <= -abs(self.config.daily_loss_limit_pct):
                return False, f"Límite Diario ({daily_pnl_pct:.2f}%)"

        if len(self.state.daily_trade_stats) >= self.max_daily_trades:
            return False, "Max Trades Diarios"

        return True, "OK"

    async def seek_new_trade(self, kline):
        current_price = float(kline["c"])
        
        can_open, reason = await self.can_trade("CHECK", current_price)
        if not can_open: return

        # Requerimos Pivotes y ATR (para el Stop Loss)
        p = self.state.daily_pivots
        if not p:
            if time.time() % 60 < 2: logging.info(f"[{self.config.symbol}] Esperando pivotes...")
            return
        
        atr = self.state.cached_atr
        if not atr: return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_volume = float(kline["q"])
                median_vol = self.state.cached_median_vol
                
                # --- FILTRO MÍNIMO DE VOLUMEN ---
                # Solo queremos que haya "algo" de interés, no una explosión.
                # Factor 1.0 significa "Volumen normal o superior".
                vol_factor = 1.0 
                if median_vol and current_volume < (median_vol * vol_factor):
                    return

                # --- LÓGICA PIVOT BOSS (FRANK OCHOA) ---
                
                # 1. Definir Régimen del Día por Ancho de CPR
                cpr_width = p.get("width", 0)
                is_narrow_cpr = cpr_width < 0.25  # Umbral clásico de Ochoa para BTC/ETH
                
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                # Velas de color (Confirmación básica)
                is_green = current_price > open_price
                is_red = current_price < open_price

                # --- ESCENARIO A: CPR ESTRECHO (Día de Tendencia/Breakout) ---
                if is_narrow_cpr:
                    # Buscamos SOLO rupturas de H4 o L4.
                    # Ignoramos L3/H3 porque en días de tendencia suelen ser arrollados.
                    
                    # Breakout Long (Rompe H4)
                    if current_price > p["H4"] and is_green:
                        side, entry_type = SIDE_BUY, "Boss Breakout Long"
                        sl = current_price - atr # SL a 1 ATR
                        # Target: H5 o Expansión
                        target = p.get("H5", current_price + (atr * 3))
                        tp_prices = [target]

                    # Breakout Short (Rompe L4)
                    elif current_price < p["L4"] and is_red:
                        side, entry_type = SIDE_SELL, "Boss Breakout Short"
                        sl = current_price + atr
                        # Target: L5 o Expansión
                        target = p.get("L5", current_price - (atr * 3))
                        tp_prices = [target]

                # --- ESCENARIO B: CPR ANCHO (Día de Rango/Reversión) ---
                else:
                    # Buscamos SOLO reversiones en L3 o H3.
                    # Ignoramos rupturas porque suelen ser falsas (Fakeouts) en días de rango.
                    
                    # Reversión Long (Rebote en L3)
                    # El precio toca o perfora L3 pero cierra o está arriba (compra el soporte)
                    # Zona de compra: Entre L4 y L3
                    if p["L4"] < current_price <= p["L3"] and is_green:
                        side, entry_type = SIDE_BUY, "Boss Reversal Long"
                        # SL: Debajo de L4 (el soporte crítico)
                        sl = p["L4"] - (atr * 0.2) 
                        # Target: Regreso a la media (CPR Central / P)
                        tp_prices = [p["P"]]

                    # Reversión Short (Rebote en H3)
                    # Zona de venta: Entre H3 y H4
                    elif p["H3"] <= current_price < p["H4"] and is_red:
                        side, entry_type = SIDE_SELL, "Boss Reversal Short"
                        # SL: Arriba de H4 (la resistencia crítica)
                        sl = p["H4"] + (atr * 0.2)
                        # Target: Regreso a la media (CPR Central / P)
                        tp_prices = [p["P"]]

                # --- EJECUCIÓN ---
                if side:
                    # Gestión de riesgo básica
                    balance = await self.bot._get_account_balance()
                    if not balance: return
                    
                    invest = balance * self.config.investment_pct
                    notional = invest * self.config.leverage
                    qty = float(format_qty(self.config.step_size, notional / current_price))
                    if qty <= 0: return
                    
                    # Formatear TPs
                    tps_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices]
                    
                    logging.info(f"!!! SEÑAL PIVOT BOSS !!! {entry_type} (CPR: {cpr_width:.2f}%)")
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tps_fmt, entry_type)

            except Exception as e:
                logging.error(f"[{self.config.symbol}] Seek Error: {e}", exc_info=True)

    # --- MÉTODOS DE GESTIÓN DE POSICIÓN (Mantenidos igual para seguridad) ---
    
    async def check_position_state(self):
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                qty = abs(float(pos.get("positionAmt", 0)))
                
                # Detectar cierre externo
                if qty < 0.0001:
                    if self.state.is_in_position:
                        await self._handle_full_close()
                    return 
                
                # Detectar apertura
                if not self.state.is_in_position and qty > 0:
                    self.state.is_in_position = True
                    self.state.current_position_info = {
                        "quantity": qty, 
                        "entry_price": float(pos.get("entryPrice")),
                        "side": SIDE_BUY if float(pos.get("positionAmt")) > 0 else SIDE_SELL,
                        "tps_hit_count": 0,
                        "entry_time": self._get_now()
                    }
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    return

                # Monitoreo
                current_pnl = float(pos.get("unRealizedProfit"))
                self.state.current_position_info['unrealized_pnl'] = current_pnl
                
                # Detectar TP parcial
                if qty < self.state.last_known_position_qty:
                    await self._handle_partial_tp(qty)
                
                # Trailing Stop Simple (Opcional, Ochoa usa Targets fijos, pero proteger ganancia es bueno)
                # Solo activamos BE si ya estamos en ganancia decente (1 ATR)
                entry_price = self.state.current_position_info["entry_price"]
                mark_price = float(pos.get("markPrice"))
                atr = self.state.cached_atr
                
                if atr:
                    side = self.state.current_position_info["side"]
                    dist = (mark_price - entry_price) if side == SIDE_BUY else (entry_price - mark_price)
                    
                    if dist > atr and not self.state.sl_moved_to_be:
                        await self.orders_manager.move_sl_to_be(qty)

                # Time Stop (Limpieza) - 24h máx
                entry_time = self.state.current_position_info.get("entry_time", 0)
                if (self._get_now() - entry_time) > 86400:
                     await self.orders_manager.close_position_manual(reason="Time Stop 24h")

            except Exception as e:
                if "1003" not in str(e): logging.error(f"Check Error: {e}")

    async def _handle_full_close(self):
        # Limpieza estándar post-trade
        try: await self.client.futures_cancel_all_open_orders(symbol=self.config.symbol)
        except: pass
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.last_known_position_qty = 0.0
        self.state.sl_moved_to_be = False
        self.state.save_state()
        
        # Log PnL (Simplificado)
        try:
            last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
            pnl = float(last_trade.get("realizedPnl", 0.0))
            self.state.daily_trade_stats.append({"pnl": pnl})
            
            icon = "✅" if pnl > 0 else "🛑"
            await self.telegram_handler._send_message(f"{icon} <b>Trade Cerrado</b> | PnL: {pnl:.2f}")
        except: pass

    async def _handle_partial_tp(self, qty):
        self.state.last_known_position_qty = qty
        self.state.save_state()
        await self.telegram_handler._send_message(f"🎯 <b>TP Parcial Ejecutado</b>")