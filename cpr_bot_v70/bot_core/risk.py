import logging
import time
import csv
import os
from datetime import datetime
from binance.exceptions import BinanceAPIException
    
from .utils import (
    format_price, format_qty, CSV_HEADER,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)

class RiskManager:
    def __init__(self, bot_controller):
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.telegram_handler = bot_controller.telegram_handler
        self.config = bot_controller

    async def seek_new_trade(self, kline):
        if self.state.trading_paused: return
        if time.time() < self.state.trade_cooldown_until: return
        if not self.state.daily_pivots: return
        
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            return
        
        async with self.bot.lock:
            if self.state.is_in_position: return
            
            try:
                open_price = float(kline["o"])
                current_price = float(kline["c"])
                current_volume = float(kline["q"]) # USDT
                
                is_green_candle = current_price > open_price
                is_red_candle = current_price < open_price
                
                # --- NUEVO: Filtro de Volatilidad ---
                atr = self.state.cached_atr
                atr_pct = (atr / current_price) * 100
                if atr_pct < self.config.min_volatility_atr_pct:
                    return
                # -----------------------------------

                median_vol = self.state.cached_median_vol
                if not median_vol or median_vol == 0: return
                
                required_volume = median_vol * self.config.volume_factor
                volume_confirmed = current_volume > required_volume
                
                p = self.state.daily_pivots
                ema = self.state.cached_ema
                side = None
                entry_type = None
                sl = None
                tp_prices = []
                
                if current_price > p["H4"]:
                    if volume_confirmed and current_price > ema and is_green_candle:
                        side, entry_type = SIDE_BUY, "Breakout Long"
                        sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG H4] Rechazado. Vol: {volume_confirmed}, EMA: {current_price>ema}, VelaVerde: {is_green_candle}")
                
                elif current_price < p["L4"]:
                    if volume_confirmed and current_price < ema and is_red_candle:
                        side, entry_type = SIDE_SELL, "Breakout Short"
                        sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG L4] Rechazado. Vol: {volume_confirmed}, EMA: {current_price<ema}, VelaRoja: {is_red_candle}")
                
                elif current_price <= p["L3"]:
                    if volume_confirmed and is_green_candle:
                        side, entry_type = SIDE_BUY, "Ranging Long"
                        sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                        tp_prices = [current_price + (atr * 0.5), current_price + (atr * 1.0), current_price + (atr * 2.0)]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG L3] Rechazado. Vol: {volume_confirmed}, VelaVerde: {is_green_candle}")

                elif current_price >= p["H3"]:
                    if volume_confirmed and is_red_candle:
                        side, entry_type = SIDE_SELL, "Ranging Short"
                        sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                        tp_prices = [current_price - (atr * 0.5), current_price - (atr * 1.0), current_price - (atr * 2.0)]
                    else:
                        logging.info(f"[{self.config.symbol}] [DEBUG H3] Rechazado. Vol: {volume_confirmed}, VelaRoja: {is_red_candle}")
                
                if side:
                    balance = await self.bot._get_account_balance()
                    if balance is None: return
                    if await self._daily_loss_exceeded(balance):
                        await self.telegram_handler._send_message(f"❌ <b>{self.config.symbol}</b>: Límite de pérdida diaria alcanzado.")
                        self.state.trade_cooldown_until = time.time() + 86400
                        return
                    
                    invest = balance * self.config.investment_pct
                    qty_raw = (invest * self.config.leverage) / current_price
                    qty = float(format_qty(self.config.step_size, qty_raw))
                    
                    if qty <= 0:
                        logging.warning(f"[{self.config.symbol}] Cantidad calculada es 0.")
                        return
                    
                    if entry_type.startswith("Breakout"): tp_prices = [tp_prices[0]]
                    tp_prices_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices if tp is not None]
                    
                    logging.info(f"!!! SEÑAL {self.config.symbol} !!! {entry_type}")
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
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position()
                if not pos: return
                qty = abs(float(pos.get("positionAmt", 0)))
                
                if not self.state.is_in_position:
                    if qty > 0:
                        logging.info(f"[{self.config.symbol}] Posición detectada; sincronizando.")
                        self.state.is_in_position = True
                        self.state.current_position_info = {
                            "quantity": qty, "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0, "entry_time": time.time(), "total_pnl": 0.0,
                            "mark_price": float(pos.get("markPrice", 0.0)),
                            "unrealized_pnl": float(pos.get("unRealizedProfit", 0.0)),
                        }
                        self.state.last_known_position_qty = qty
                        self.state.save_state()
                    return 

                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice", 0.0))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit", 0.0))
                
                if qty == 0:
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
                    return 
                
                if qty < self.state.last_known_position_qty:
                    partial_pnl = 0.0
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
                        partial_pnl = float(last_trade.get("realizedPnl", 0.0))
                    except Exception: pass
                    
                    tp_hit_count = self.state.current_position_info.get("tps_hit_count", 0) + 1
                    self.state.current_position_info["tps_hit_count"] = tp_hit_count
                    self.state.current_position_info["total_pnl"] = self.state.current_position_info.get("total_pnl", 0) + partial_pnl
                    
                    logging.info(f"[{self.config.symbol}] TP PARCIAL (TP{tp_hit_count}). PnL: {partial_pnl}")
                    await self.telegram_handler._send_message(f"🎯 <b>{self.config.symbol} TP{tp_hit_count}</b>\nPnL: <code>{partial_pnl:+.2f}</code> | Qty: {qty}")
                    
                    self.state.last_known_position_qty = qty
                    self.state.save_state()
                    
                    if tp_hit_count == 2 and not self.state.sl_moved_to_be:
                        await self.orders_manager.move_sl_to_be(qty)
                
                # --- NUEVO: TRAILING STOP ---
                # (Mover SL dinámicamente si el precio avanza)
                await self._check_trailing_stop(float(pos.get("markPrice", 0.0)), qty)

                # Time Stop
                if (not self.state.sl_moved_to_be and 
                    self.state.current_position_info.get("entry_type", "").startswith("Ranging")):
                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0 and (time.time() - entry_time) / 3600 > 12:
                        logging.warning(f"[{self.config.symbol}] TIME STOP (12h). Cerrando.")
                        await self.telegram_handler._send_message(f"⏳ <b>{self.config.symbol} TIME STOP</b>")
                        await self.orders_manager.close_position_manual(reason="Time Stop 12h")
            
            except BinanceAPIException as e:
                if e.code != -1003: logging.error(f"[{self.config.symbol}] Error API: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"[{self.config.symbol}] Error check_position: {e}", exc_info=True)

    async def _check_trailing_stop(self, current_price, qty):
        info = self.state.current_position_info
        entry_price = info.get('entry_price', 0)
        side = info.get('side')
        atr = self.state.cached_atr
        
        if not atr: return

        trigger_dist = atr * self.config.trailing_stop_trigger_atr
        trail_dist = atr * self.config.trailing_stop_distance_atr
        
        new_sl_price = None
        
        if side == SIDE_BUY:
            if current_price > (entry_price + trigger_dist):
                potential_sl = current_price - trail_dist
                current_sl = info.get("trailing_sl_price", entry_price)
                if potential_sl > current_sl:
                    new_sl_price = potential_sl
        elif side == SIDE_SELL:
            if current_price < (entry_price - trigger_dist):
                potential_sl = current_price + trail_dist
                current_sl = info.get("trailing_sl_price", entry_price)
                # En Short, un SL mejor es un precio MENOR
                # Si el SL es BE (entry), cualquier precio menor es ganancia
                # Pero cuidado: En Short, stop price baja. 
                # Queremos que el nuevo SL sea MENOR que el actual si ya estamos en ganancia?
                # NO. En Short, el SL baja persiguiendo el precio.
                # Si current_sl es entry (100), y potential es 90, 90 es mejor.
                # Si no hay trailing previo, current_sl puede ser entry o el SL inicial.
                # Asumiremos que si ya movimos a BE, current_sl es <= entry.
                if potential_sl < current_sl:
                    new_sl_price = potential_sl
        
        if new_sl_price:
            logging.info(f"[{self.config.symbol}] Trailing SL activado/actualizado a {new_sl_price:.2f}")
            # Actualizar en Binance
            await self.orders_manager.update_sl(new_sl_price, qty, side)
            # Actualizar estado
            self.state.current_position_info["trailing_sl_price"] = new_sl_price
            self.state.save_state()
