import logging
import time
import csv
import os
from datetime import datetime
from binance.exceptions import BinanceAPIException
    
# Importar nuestras constantes y formateadores desde utils
from .utils import (
    format_price, format_qty, CSV_HEADER,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)

class RiskManager:
    def __init__(self, bot_controller):
        """
        Inicializa el gestor de riesgo y estrategia.
        :param bot_controller: La instancia de SymbolStrategy (que actúa como config y controlador).
        """
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.telegram_handler = bot_controller.telegram_handler
        self.config = bot_controller # La estrategia tiene los atributos de config (ema_period, etc.)

    async def seek_new_trade(self, kline):
        """
        Lógica principal de entrada. Evalúa precio, volumen, EMA y color de vela.
        """
        # Filtros iniciales de estado
        if self.state.trading_paused: return
        if time.time() < self.state.trade_cooldown_until: return
        if not self.state.daily_pivots: return
        
        # Verificar que los indicadores existan
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            logging.debug(f"[{self.config.symbol}] Indicadores no listos para operar.")
            return
        
        # Usar el lock del bot para evitar condiciones de carrera
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                # --- Datos de la Vela Actual ---
                open_price = float(kline["o"])
                current_price = float(kline["c"])
                current_volume = float(kline["q"]) # Volumen en USDT (Quote Asset)
                
                # Dirección de la vela (Filtro de Confirmación)
                is_green_candle = current_price > open_price
                is_red_candle = current_price < open_price
                
                # --- Filtro de Volumen (Mediana) ---
                median_vol = self.state.cached_median_vol
                if not median_vol or median_vol == 0:
                    logging.debug("Volumen mediano (1m, USDT) es 0 o None")
                    return
                
                required_volume = median_vol * self.config.volume_factor
                volume_confirmed = current_volume > required_volume
                
                # --- Datos de Estrategia ---
                p = self.state.daily_pivots
                atr = self.state.cached_atr
                ema = self.state.cached_ema
                
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                # ==========================================
                #        LÓGICA DE ENTRADA (v71)
                # ==========================================
                
                # 1. Breakout Long (H4)
                # Requiere: Precio > H4 + Volumen + Precio > EMA + Vela Verde
                if current_price > p["H4"]:
                    if volume_confirmed and current_price > ema and is_green_candle:
                        side, entry_type = SIDE_BUY, "Breakout Long"
                        sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                    else:
                        # Log de rechazo para depuración
                        logging.info(f"[{self.config.symbol}] [DEBUG H4] Rechazado. Vol: {volume_confirmed}, EMA: {current_price > ema}, VelaVerde: {is_green_candle}")
                
                # 2. Breakout Short (L4)
                # Requiere: Precio < L4 + Volumen + Precio < EMA + Vela Roja
                elif current_price < p["L4"]:
                    if volume_confirmed and current_price < ema and is_red_candle:
                        side, entry_type = SIDE_SELL, "Breakout Short"
                        sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG L4] Rechazado. Vol: {volume_confirmed}, EMA: {current_price < ema}, VelaRoja: {is_red_candle}")
                
                # 3. Ranging Long (L3) - Reversión a la media
                # Requiere: Precio <= L3 + Volumen + Vela Verde (NO requiere EMA)
                elif current_price <= p["L3"]:
                    if volume_confirmed and is_green_candle:
                        side, entry_type = SIDE_BUY, "Ranging Long"
                        sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                        tp_prices = [p["P"], p["H1"], p["H2"]]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG L3] Rechazado. Precio OK. Vol: {volume_confirmed}, VelaVerde: {is_green_candle}")

                # 4. Ranging Short (H3) - Reversión a la media
                # Requiere: Precio >= H3 + Volumen + Vela Roja (NO requiere EMA)
                elif current_price >= p["H3"]:
                    if volume_confirmed and is_red_candle:
                        side, entry_type = SIDE_SELL, "Ranging Short"
                        sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                        tp_prices = [p["P"], p["L1"], p["L2"]]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG H3] Rechazado. Precio OK. Vol: {volume_confirmed}, VelaRoja: {is_red_candle}")
                
                # ==========================================
                #          EJECUCIÓN DE ORDEN
                # ==========================================

                if side:
                    # 1. Verificar Balance y Límite de Pérdida
                    balance = await self.bot._get_account_balance()
                    if balance is None: return
                    
                    if await self._daily_loss_exceeded(balance):
                        await self.telegram_handler._send_message(f"❌ <b>{self.config.symbol}</b>: Límite de pérdida diaria alcanzado. Trading pausado.")
                        self.state.trade_cooldown_until = time.time() + 86400 # Pausa 24h
                        return
                    
                    # 2. Calcular Cantidad
                    invest = balance * self.config.investment_pct
                    qty_raw = (invest * self.config.leverage) / current_price
                    qty = float(format_qty(self.config.step_size, qty_raw))
                    
                    if qty <= 0:
                        logging.warning(f"[{self.config.symbol}] Cantidad calculada es 0. Saldo insuficiente o step_size muy alto.")
                        return
                    
                    # 3. Formatear TPs
                    # Si es breakout, solo usamos el primer TP calculado dinámicamente
                    if entry_type.startswith("Breakout"):
                        tp_prices = [tp_prices[0]]
                    
                    tp_prices_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices if tp is not None]
                    
                    logging.info(f"!!! SEÑAL {self.config.symbol} !!! {entry_type} {side} ; qty {qty} ; SL {sl} ; TPs {tp_prices_fmt}")
                    
                    # 4. Colocar Orden (Delegar al OrdersManager)
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tp_prices_fmt, entry_type)

            except Exception as e:
                logging.error(f"[{self.config.symbol}] Error en seek_new_trade: {e}", exc_info=True)

    async def _daily_loss_exceeded(self, balance):
        """Comprueba si se ha superado el límite de pérdida diaria."""
        if balance <= 0: 
            return False # Evitar falsos positivos si no hay saldo inicial
        
        # PnL realizado hoy + PnL flotante actual (si lo hubiera, aunque al buscar trade no debería haber)
        total_pnl = self.state.current_position_info.get("total_pnl", 0)
        total_pnl += sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        
        # Si estamos en positivo, todo bien
        if total_pnl >= 0:
            return False

        loss_limit = -abs((self.config.daily_loss_limit_pct / 100.0) * balance)
        
        # True si la pérdida es mayor (más negativa) que el límite
        return total_pnl <= loss_limit

    async def check_position_state(self):
        """
        El corazón de la gestión de posición. Comprueba TPs, SLs, y Time Stops.
        Llamado por el Poller (5s) y el User Data Stream (instantáneo).
        """
        async with self.bot.lock:
            try:
                # Obtener posición en vivo de Binance
                pos = await self.bot._get_current_position()
                if not pos: return

                qty = abs(float(pos.get("positionAmt", 0)))
                
                # --- CASO 1: El bot cree que NO tiene posición, pero Binance dice que SÍ ---
                if not self.state.is_in_position:
                    if qty > 0:
                        # Reconciliación (Recuperación de estado tras reinicio o fallo)
                        logging.info(f"[{self.config.symbol}] Posición detectada por poller; sincronizando estado.")
                        self.state.is_in_position = True
                        self.state.current_position_info = {
                            "quantity": qty,
                            "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0,
                            "entry_time": time.time(), # Asumimos tiempo actual si no hay registro
                            "total_pnl": 0.0,
                            "mark_price": float(pos.get("markPrice", 0.0)),
                            "unrealized_pnl": float(pos.get("unRealizedProfit", 0.0)),
                        }
                        self.state.last_known_position_qty = qty
                        await self.telegram_handler._send_message(f"🔁 <b>{self.config.symbol}</b>: Posición detectada y sincronizada.")
                        self.state.save_state()
                    return 

                # --- CASO 2: El bot sabe que tiene posición ---
                
                # Actualizar datos en vivo para el /status
                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice", 0.0))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit", 0.0))
                
                # --- A. Detección de CIERRE TOTAL ---
                if qty == 0:
                    logging.info(f"[{self.config.symbol}] Posición cerrada detectada (qty 0).")
                    pnl, close_px, roi = 0.0, 0.0, 0.0
                    
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
                        pnl = float(last_trade.get("realizedPnl", 0.0))
                        close_px = float(last_trade.get("price", 0.0))
                    except Exception as e:
                        logging.error(f"Error al obtener último trade para PnL: {e}")

                    # Calcular totales
                    total_pnl = self.state.current_position_info.get("total_pnl", 0) + pnl
                    entry_price = self.state.current_position_info.get("entry_price", 0.0)
                    quantity_initial = self.state.current_position_info.get("quantity", 0.0)
                    
                    if entry_price > 0 and quantity_initial > 0 and self.config.leverage > 0:
                        initial_margin = (entry_price * quantity_initial) / self.config.leverage
                        if initial_margin > 0:
                            roi = (total_pnl / initial_margin) * 100

                    # Guardar en CSV
                    td = {
                        "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                        "entry_type": self.state.current_position_info.get("entry_type", "Unknown"),
                        "side": self.state.current_position_info.get("side", "Unknown"),
                        "quantity": quantity_initial,
                        "entry_price": entry_price,
                        "mark_price_entry": self.state.current_position_info.get("mark_price_entry", 0.0),
                        "close_price_avg": close_px,
                        "pnl": total_pnl,
                        "pnl_percent_roi": roi, 
                        "cpr_width": self.state.daily_pivots.get("width", 0),
                        "atr_at_entry": self.state.current_position_info.get("atr_at_entry", 0),
                        "ema_filter": self.state.current_position_info.get("ema_at_entry", 0)
                    }
                    # Usar self.bot.CSV_FILE porque es específico del símbolo
                    self.bot._log_trade_to_csv(td, self.bot.CSV_FILE)
                    
                    # Actualizar stats diarias
                    self.state.daily_trade_stats.append({"pnl": total_pnl, "roi": roi})
                    
                    # Notificar
                    icon = "✅" if total_pnl >= 0 else "❌"
                    msg = f"{icon} <b>{self.config.symbol} CERRADA</b> {icon}\n\n" \
                          f"<b>Tipo</b>: <code>{self.state.current_position_info.get('entry_type', 'N/A')}</code>\n" \
                          f"<b>PnL Total</b>: <code>{total_pnl:+.2f} USDT</code>\n" \
                          f"<b>ROI</b>: <code>{roi:+.2f}%</code> (sobre margen inicial)\n"
                    await self.telegram_handler._send_message(msg)
                    
                    # Resetear estado
                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.last_known_position_qty = 0.0
                    self.state.sl_moved_to_be = False
                    self.state.save_state()
                    return 
                
                # --- B. Detección de TP PARCIAL ---
                if qty < self.state.last_known_position_qty:
                    partial_pnl = 0.0
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
                        partial_pnl = float(last_trade.get("realizedPnl", 0.0))
                    except Exception: pass
                    
                    tp_hit_count = self.state.current_position_info.get("tps_hit_count", 0) + 1
                    self.state.current_position_info["tps_hit_count"] = tp_hit_count
                    self.state.current_position_info["total_pnl"] = self.state.current_position_info.get("total_pnl", 0) + partial_pnl
                    
                    logging.info(f"[{self.config.symbol}] TP PARCIAL (TP{tp_hit_count}). Restante: {qty}. PnL: {partial_pnl}")
                    await self.telegram_handler._send_message(f"🎯 <b>{self.config.symbol} TP{tp_hit_count}</b>\nPnL: <code>{partial_pnl:+.2f}</code> | Qty: {qty}")
                    
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    
                    # Mover SL a BE al tocar TP2
                    if tp_hit_count == 2 and not self.state.sl_moved_to_be:
                        await self.orders_manager.move_sl_to_be(qty)
                
                # --- C. Detección de TIME STOP (12 HORAS) ---
                # Solo para operaciones de RANGO (L3/H3)
                if (not self.state.sl_moved_to_be and 
                    self.state.current_position_info.get("entry_type", "").startswith("Ranging")):
                    
                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0:
                        hours_in_trade = (time.time() - entry_time) / 3600
                        
                        if hours_in_trade > 12: # <-- TIEMPO OPTIMIZADO: 12 HORAS
                            logging.warning(f"[{self.config.symbol}] TIME STOP (12h) activado. Cerrando.")
                            await self.telegram_handler._send_message(f"⏳ <b>{self.config.symbol} TIME STOP</b>\nTrade de Rango superó 12h. Cerrando.")
                            
                            await self.orders_manager.close_position_manual(reason="Time Stop 12h")
            
            except BinanceAPIException as e:
                if e.code == -1003: logging.warning("Rate limit (-1003) en check_position_state.")
                else: logging.error(f"[{self.config.symbol}] Error API en check_position_state: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"[{self.config.symbol}] Error en check_position_state: {e}", exc_info=True)
