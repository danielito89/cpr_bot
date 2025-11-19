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
        Lógica principal de entrada. Híbrida: Breakout (Prioridad) -> Rango.
        """
        # Filtros iniciales de estado
        if self.state.trading_paused: return
        if time.time() < self.state.trade_cooldown_until: return
        if not self.state.daily_pivots: return
        
        # Verificar que los indicadores existan
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            # logging.debug(f"[{self.config.symbol}] Indicadores no listos para operar.")
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
                
                # Filtro de Volatilidad Mínima (Opcional, si está configurado)
                if hasattr(self.config, 'min_volatility_atr_pct'):
                    atr_pct = (self.state.cached_atr / current_price) * 100
                    if atr_pct < self.config.min_volatility_atr_pct:
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
                #        LÓGICA DE ENTRADA (Híbrida)
                # ==========================================
                
                # 1. Breakout Long (H4)
                if current_price > p["H4"]:
                    if volume_confirmed and current_price > ema and is_green_candle:
                        side, entry_type = SIDE_BUY, "Breakout Long"
                        sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG H4] Rechazado. Vol: {volume_confirmed} (Act:{current_volume:.0f}>Req:{required_volume:.0f}), EMA: {current_price > ema}, VelaVerde: {is_green_candle}")
                
                # 2. Breakout Short (L4)
                elif current_price < p["L4"]:
                    if volume_confirmed and current_price < ema and is_red_candle:
                        side, entry_type = SIDE_SELL, "Breakout Short"
                        sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG L4] Rechazado. Vol: {volume_confirmed} (Act:{current_volume:.0f}>Req:{required_volume:.0f}), EMA: {current_price < ema}, VelaRoja: {is_red_candle}")
                
                # 3. Rango (Solo si no es Breakout)
                if not side:
                    # Ranging Long (L3)
                    if current_price <= p["L3"]:
                        if volume_confirmed and is_green_candle:
                            side, entry_type = SIDE_BUY, "Ranging Long"
                            sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                            # TPs Dinámicos (ATR)
                            tp_prices = [
                                current_price + (atr * 0.5),
                                current_price + (atr * 1.0),
                                current_price + (atr * 2.0)
                            ]
                        else:
                            logging.info(f"[{self.config.symbol}] [DEBUG L3] Rechazado. Vol: {volume_confirmed} (Act:{current_volume:.0f}>Req:{required_volume:.0f}), VelaVerde: {is_green_candle}")

                    # Ranging Short (H3)
                    elif current_price >= p["H3"]:
                        if volume_confirmed and is_red_candle:
                            side, entry_type = SIDE_SELL, "Ranging Short"
                            sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                            # TPs Dinámicos (ATR)
                            tp_prices = [
                                current_price - (atr * 0.5),
                                current_price - (atr * 1.0),
                                current_price - (atr * 2.0)
                            ]
                        else:
                            logging.info(f"[{self.config.symbol}] [DEBUG H3] Rechazado. Vol: {volume_confirmed} (Act:{current_volume:.0f}>Req:{required_volume:.0f}), VelaRoja: {is_red_candle}")
                
                # ==========================================
                #          EJECUCIÓN DE ORDEN
                # ==========================================

                if side:
                    balance = await self.bot._get_account_balance()
                    if balance is None: return
                    
                    if await self._daily_loss_exceeded(balance):
                        await self.telegram_handler._send_message(f"❌ <b>{self.config.symbol}</b>: Límite de pérdida diaria alcanzado. Trading pausado.")
                        self.state.trade_cooldown_until = time.time() + 86400 # Pausa 24h
                        return
                    
                    invest = balance * self.config.investment_pct
                    qty_raw = (invest * self.config.leverage) / current_price
                    qty = float(format_qty(self.config.step_size, qty_raw))
                    
                    if qty <= 0:
                        logging.warning(f"[{self.config.symbol}] Cantidad calculada es 0. Saldo insuficiente o step_size muy alto.")
                        return
                    
                    # Si es breakout, solo usamos el primer TP
                    if entry_type.startswith("Breakout"):
                        tp_prices = [tp_prices[0]]
                    
                    tp_prices_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices if tp is not None]
                    
                    logging.info(f"!!! SEÑAL {self.config.symbol} !!! {entry_type} {side} ; qty {qty} ; SL {sl} ; TPs {tp_prices_fmt}")
                    
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tp_prices_fmt, entry_type)

            except Exception as e:
                logging.error(f"[{self.config.symbol}] Error en seek_new_trade: {e}", exc_info=True)

    async def _daily_loss_exceeded(self, balance):
        if balance <= 0: return False
        total_pnl = self.state.current_position_info.get("total_pnl", 0)
        total_pnl += sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        if total_pnl >= 0: return False
        loss_limit = -abs((self.config.daily_loss_limit_pct / 100.0) * balance)
        return total_pnl <= loss_limit

    async def check_position_state(self):
        """
        Gestión de posición: TPs, SLs, Trailing y Time Stops.
        """
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                qty = abs(float(pos.get("positionAmt", 0)))
                
                # 1. Reconciliación (Si el bot olvidó que tenía posición)
                if not self.state.is_in_position:
                    if qty > 0:
                        logging.info(f"[{self.config.symbol}] Posición detectada; sincronizando.")
                        self.state.is_in_position = True
                        self.state.current_position_info = {
                            "quantity": qty,
                            "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0, "entry_time": time.time(), "total_pnl": 0.0,
                            "mark_price": float(pos.get("markPrice", 0.0)),
                            "unrealized_pnl": float(pos.get("unRealizedProfit", 0.0)),
                        }
                        self.state.last_known_position_qty = qty
                        await self.telegram_handler._send_message(f"🔁 <b>{self.config.symbol}</b>: Posición detectada y sincronizada.")
                        self.state.save_state()
                    return 

                # Actualizar datos en vivo
                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice", 0.0))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit", 0.0))
                
                # 2. Cierre Total (qty = 0)
                if qty == 0:
                    await self._handle_full_close()
                    return 
                
                # 3. TP Parcial (qty disminuyó)
                if qty < self.state.last_known_position_qty:
                    await self._handle_partial_tp(qty)
                
                # 4. Trailing Stop
                await self._check_trailing_stop(float(pos.get("markPrice", 0.0)), qty)

                # 5. Time Stop (12h)
                if (not self.state.sl_moved_to_be and 
                    self.state.current_position_info.get("entry_type", "").startswith("Ranging")):
                    
                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0:
                        hours = (time.time() - entry_time) / 3600
                        if hours > 12: # TimeStop 12h
                            logging.warning(f"[{self.config.symbol}] TIME STOP (12h). Cerrando.")
                            await self.telegram_handler._send_message(f"⏳ <b>{self.config.symbol} TIME STOP</b>\nTrade superó 12h.")
                            await self.orders_manager.close_position_manual(reason="Time Stop 12h")
            
            except BinanceAPIException as e:
                if e.code != -1003: logging.error(f"[{self.config.symbol}] Error API: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"[{self.config.symbol}] Error check_position: {e}", exc_info=True)

    # --- Lógica de Cierre Completo ---
    async def _handle_full_close(self):
        logging.info(f"[{self.config.symbol}] Cierre detectado.")
        pnl, close_px, roi = 0.0, 0.0, 0.0
        try:
            last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
            pnl = float(last_trade.get("realizedPnl", 0.0))
            close_px = float(last_trade.get("price", 0.0))
        except Exception: pass

        total_pnl = self.state.current_position_info.get("total_pnl", 0) + pnl
        entry_price = self.state.current_position_info.get("entry_price", 0.0)
        quantity = self.state.current_position_info.get("quantity", 0.0)
        
        if entry_price > 0 and quantity > 0 and self.config.leverage > 0:
            initial_margin = (entry_price * quantity) / self.config.leverage
            roi = (total_pnl / initial_margin) * 100 if initial_margin > 0 else 0

        td = {
            "timestamp_utc": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_type": self.state.current_position_info.get("entry_type", "Unknown"),
            "side": self.state.current_position_info.get("side", "Unknown"),
            "quantity": quantity, "entry_price": entry_price,
            "mark_price_entry": self.state.current_position_info.get("mark_price_entry", 0.0),
            "close_price_avg": close_px, "pnl": total_pnl, "pnl_percent_roi": roi, 
            "cpr_width": self.state.daily_pivots.get("width", 0),
            "atr_at_entry": self.state.current_position_info.get("atr_at_entry", 0),
            "ema_filter": self.state.current_position_info.get("ema_at_entry", 0)
        }
        self.bot._log_trade_to_csv(td, self.bot.CSV_FILE)
        self.state.daily_trade_stats.append({"pnl": total_pnl, "roi": roi})
        
        icon = "✅" if total_pnl >= 0 else "❌"
        msg = f"{icon} <b>{self.config.symbol} CERRADA</b> {icon}\n\n" \
              f"<b>Tipo</b>: <code>{self.state.current_position_info.get('entry_type', 'N/A')}</code>\n" \
              f"<b>PnL Total</b>: <code>{total_pnl:+.2f} USDT</code>\n" \
              f"<b>ROI</b>: <code>{roi:+.2f}%</code>\n"
        await self.telegram_handler._send_message(msg)
        
        self.state.is_in_position = False
        self.state.current_position_info = {}
        self.state.last_known_position_qty = 0.0
        self.state.sl_moved_to_be = False
        self.state.save_state()

    # --- Lógica de TP Parcial ---
    async def _handle_partial_tp(self, qty):
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

    # --- Lógica Trailing Stop ---
    async def _check_trailing_stop(self, current_price, qty):
        info = self.state.current_position_info
        entry_price = info.get('entry_price', 0)
        side = info.get('side')
        atr = self.state.cached_atr
        
        if not atr: return

        # Usar parámetros de configuración
        trigger_dist = atr * self.config.trailing_stop_trigger_atr
        trail_dist = atr * self.config.trailing_stop_distance_atr
        
        new_sl_price = None
        
        if side == SIDE_BUY:
            if current_price > (entry_price + trigger_dist):
                potential_sl = current_price - trail_dist
                # FIX: Manejar None en trailing_sl_price
                current_sl = info.get("trailing_sl_price")
                if current_sl is None: current_sl = entry_price # Si no hay, usar BE
                
                if potential_sl > current_sl:
                    new_sl_price = potential_sl

        elif side == SIDE_SELL:
            if current_price < (entry_price - trigger_dist):
                potential_sl = current_price + trail_dist
                # FIX: Manejar None
                current_sl = info.get("trailing_sl_price")
                if current_sl is None: current_sl = entry_price
                
                if potential_sl < current_sl:
                    new_sl_price = potential_sl
        
        if new_sl_price:
            logging.info(f"[{self.config.symbol}] Actualizando Trailing SL a {new_sl_price:.2f}")
            await self.orders_manager.update_sl(new_sl_price, qty, "Trailing")
            self.state.current_position_info["trailing_sl_price"] = new_sl_price
            self.state.save_state()
