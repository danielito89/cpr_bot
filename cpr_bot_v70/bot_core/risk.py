import logging
import time
import csv
from binance.exceptions import BinanceAPIException

# Importar nuestras constantes y formateadores
from .utils import (
    format_price, format_qty, CSV_HEADER,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)

class RiskManager:
    def __init__(self, bot_controller):
        """
        Inicializa el gestor de riesgo y estrategia.
        Necesita una referencia al bot principal para acceder a todo.
        """
        self.bot = bot_controller
        self.client = bot_controller.client
        self.state = bot_controller.state
        self.orders_manager = bot_controller.orders_manager
        self.telegram_handler = bot_controller.telegram_handler
        self.config = bot_controller # El bot principal tiene la config

    async def seek_new_trade(self, kline):
        """Lógica de búsqueda de nuevas operaciones."""
        if self.state.trading_paused: return
        if time.time() < self.state.trade_cooldown_until: return
        if not self.state.daily_pivots: return
        if not all([self.state.cached_atr, self.state.cached_ema, self.state.cached_median_vol]):
            logging.debug("Indicators not ready")
            return

        # Usar el lock del bot principal
        async with self.bot.lock:
            if self.state.is_in_position: return

            try:
                current_price = float(kline["c"])
                current_volume = float(kline["q"]) # Volumen USDT

                median_vol = self.state.cached_median_vol
                if not median_vol or median_vol == 0:
                    logging.debug("median vol (1m, USDT) es 0 o None")
                    return

                required_volume = median_vol * self.config.volume_factor
                volume_confirmed = current_volume > required_volume

                p = self.state.daily_pivots
                atr = self.state.cached_atr
                ema = self.state.cached_ema
                side, entry_type, sl, tp_prices = None, None, None, []

                if current_price > p["H4"]:
                    if volume_confirmed and current_price > ema:
                        side, entry_type = SIDE_BUY, "Breakout Long"
                        sl = current_price - atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price + atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[DEBUG H4] Rechazado. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f}), EMA: {current_price > ema}")

                elif current_price < p["L4"]:
                    if volume_confirmed and current_price < ema:
                        side, entry_type = SIDE_SELL, "Breakout Short"
                        sl = current_price + atr * self.config.breakout_atr_sl_multiplier
                        tp_prices = [current_price - atr * self.config.breakout_tp_mult]
                    else:
                        logging.info(f"[DEBUG L4] Rechazado. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f}), EMA: {current_price < ema}")

                elif current_price <= p["L3"]:
                    if volume_confirmed:
                        side, entry_type = SIDE_BUY, "Ranging Long"
                        sl = p["L4"] - atr * self.config.ranging_atr_multiplier
                        tp_prices = [p["P"], p["H1"], p["H2"]]
                    else:
                        logging.info(f"[DEBUG L3] Rechazado. Precio OK. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f})")

                elif current_price >= p["H3"]:
                    if volume_confirmed:
                        side, entry_type = SIDE_SELL, "Ranging Short"
                        sl = p["H4"] + atr * self.config.ranging_atr_multiplier
                        tp_prices = [p["P"], p["L1"], p["L2"]]
                    else:
                        logging.info(f"[DEBUG H3] Rechazado. Precio OK. Vol: {volume_confirmed} (Actual: {current_volume:.0f} > Req: {required_volume:.0f})")

                if side:
                    balance = await self.bot._get_account_balance() # Llama al método del bot
                    if balance is None: return
                    if await self._daily_loss_exceeded(balance):
                        await self.telegram_handler._send_message("❌ <b>Daily loss limit reached</b> — trading paused.")
                        self.state.trade_cooldown_until = time.time() + 86400
                        return

                    invest = balance * self.config.investment_pct
                    qty = float(format_qty(self.config.step_size, (invest * self.config.leverage) / current_price))
                    if qty <= 0:
                        logging.warning("Qty computed 0; skip")
                        return

                    if entry_type.startswith("Breakout"):
                        tp_prices = [tp_prices[0]]

                    tp_prices_fmt = [float(format_price(self.config.tick_size, tp)) for tp in tp_prices if tp is not None]

                    logging.info(f"!!! SEÑAL !!! {entry_type} {side} ; qty {qty} ; SL {sl} ; TPs {tp_prices_fmt}")
                    # Llama al OrdersManager
                    await self.orders_manager.place_bracket_order(side, qty, current_price, sl, tp_prices_fmt, entry_type)

            except Exception as e:
                logging.error(f"seek_new_trade error: {e}", exc_info=True)

    async def _daily_loss_exceeded(self, balance):
        """Comprueba si se ha superado el límite de pérdida diaria."""
        total_pnl = self.state.current_position_info.get("total_pnl", 0)
        total_pnl += sum(t.get("pnl", 0) for t in self.state.daily_trade_stats)
        loss_limit = -abs((self.config.daily_loss_limit_pct / 100.0) * balance)
        return total_pnl <= loss_limit

    async def check_position_state(self):
        """
        El corazón de la gestión de posición. Comprueba TPs, SLs, y Time Stops.
        Llamado por el poller y el User Data Stream.
        """
        async with self.bot.lock:
            try:
                pos = await self.bot._get_current_position() # Llama al método del bot
                if not pos: return

                qty = abs(float(pos.get("positionAmt", 0)))

                if not self.state.is_in_position:
                    if qty > 0:
                        # RECONCILIACIÓN
                        logging.info("Detected open position by poll; syncing state")
                        self.state.is_in_position = True
                        self.state.current_position_info = {
                            "quantity": qty, "entry_price": float(pos.get("entryPrice", 0.0)),
                            "side": SIDE_BUY if float(pos.get("positionAmt", 0)) > 0 else SIDE_SELL,
                            "tps_hit_count": 0, "entry_time": time.time(), "total_pnl": 0.0,
                            "mark_price": float(pos.get("markPrice", 0.0)),
                            "unrealized_pnl": float(pos.get("unRealizedProfit", 0.0)),
                        }
                        self.state.last_known_position_qty = qty
                        await self.telegram_handler._send_message("🔁 Posición detectada por poll; bot sincronizado.")
                        self.state.save_state()
                    return 

                # --- LÓGICA 2: Estamos en posición (según el bot) ---

                if qty > 0:
                    self.state.current_position_info['mark_price'] = float(pos.get("markPrice", 0.0))
                    self.state.current_position_info['unrealized_pnl'] = float(pos.get("unRealizedProfit", 0.0))

                if qty == 0:
                    # --- DETECCIÓN DE CIERRE TOTAL ---
                    logging.info("Posición cerrada detectada (qty 0).")
                    pnl, close_px, roi = 0.0, 0.0, 0.0
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
                        pnl = float(last_trade.get("realizedPnl", 0.0))
                        close_px = float(last_trade.get("price", 0.0))
                    except Exception as e:
                        logging.error(f"Error al obtener último trade para PnL: {e}")

                    total_pnl = self.state.current_position_info.get("total_pnl", 0) + pnl
                    entry_price = self.state.current_position_info.get("entry_price", 0.0)
                    quantity = self.state.current_position_info.get("quantity", 0.0)

                    if entry_price > 0 and quantity > 0 and self.config.leverage > 0:
                        initial_margin = (entry_price * quantity) / self.config.leverage
                        if initial_margin > 0:
                            roi = (total_pnl / initial_margin) * 100

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
                    self._log_trade_to_csv(td, self.config.CSV_FILE)
                    self.state.daily_trade_stats.append({"pnl": total_pnl, "roi": roi})

                    icon = "✅" if total_pnl >= 0 else "❌"
                    msg = f"{icon} <b>POSICIÓN CERRADA</b> {icon}\n\n" \
                          f"<b>Tipo</b>: <code>{self.state.current_position_info.get('entry_type', 'N/A')}</code>\n" \
                          f"<b>PnL Total</b>: <code>{total_pnl:+.2f} USDT</code>\n" \
                          f"<b>ROI</b>: <code>{roi:+.2f}%</code> (sobre margen inicial)\n"
                    await self.telegram_handler._send_message(msg)

                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.last_known_position_qty = 0.0
                    self.state.sl_moved_to_be = False
                    self.state.save_state()
                    return 

                if qty < self.state.last_known_position_qty:
                    # --- DETECCIÓN DE TP PARCIAL ---
                    partial_pnl = 0.0
                    try:
                        last_trade = (await self.client.futures_account_trades(symbol=self.config.symbol, limit=1))[0]
                        partial_pnl = float(last_trade.get("realizedPnl", 0.0))
                    except Exception: pass

                    tp_hit_count = self.state.current_position_info.get("tps_hit_count", 0) + 1
                    self.state.current_position_info["tps_hit_count"] = tp_hit_count
                    self.state.current_position_info["total_pnl"] = self.state.current_position_info.get("total_pnl", 0) + partial_pnl

                    logging.info(f"TP PARCIAL ALCANZADO (TP{tp_hit_count}). Qty restante: {qty}. PnL: {partial_pnl}")
                    await self.telegram_handler._send_message(f"🎯 <b>TP{tp_hit_count} ALCANZADO</b>\nPnL: <code>{partial_pnl:+.2f}</code> | Qty restante: {qty}")

                    self.state.last_known_position_qty = qty
                    self.state.save_state()

                    if tp_hit_count == 2 and not self.state.sl_moved_to_be:
                        await self.orders_manager.move_sl_to_be(qty)

                # --- DETECCIÓN DE TIME STOP (6 HORAS) ---
                if (not self.state.sl_moved_to_be and 
                    self.state.current_position_info.get("entry_type", "").startswith("Ranging")):

                    entry_time = self.state.current_position_info.get("entry_time", 0)
                    if entry_time > 0 and (time.time() - entry_time) / 3600 > 6:
                        logging.warning(f"TIME STOP (6h) triggered for Ranging trade. Closing position.")
                        await self.telegram_handler._send_message(f"⏳ <b>CIERRE POR TIEMPO</b>\nTrade de Rango superó 6h. Cerrando.")
                        await self.orders_manager.close_position_manual(reason="Time Stop 6h")

            except BinanceAPIException as e:
                if e.code == -1003: logging.warning("Rate limit (-1003) en check_position_state.")
                else: logging.error(f"Error de API en check_position_state: {e}", exc_info=True)
            except Exception as e:
                logging.error(f"Error en check_position_state: {e}", exc_info=True)

    def _log_trade_to_csv(self, trade_data, csv_file_path):
        """Log de un trade cerrado a un archivo CSV."""
        file_exists = os.path.isfile(csv_file_path)
        try:
            with open(csv_file_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_HEADER)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(trade_data)
            logging.info("Trade cerrado guardado en CSV.")
        except Exception as e:
            logging.error(f"Error al guardar CSV: {e}")
