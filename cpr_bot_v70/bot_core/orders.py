import logging
import time
from decimal import Decimal
from binance.exceptions import BinanceAPIException

# Importar nuestras constantes y formateadores
from .utils import (
    format_price, format_qty,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)

class OrdersManager:
    def __init__(self, client, state, telegram_handler, config):
        """
        Inicializa el gestor de órdenes.

        :param client: La instancia de AsyncClient de Binance.
        :param state: La instancia de StateManager.
        :param telegram_handler: La instancia de TelegramHandler.
        :param config: Un diccionario o objeto con configuraciones (tick_size, etc.).
        """
        self.client = client
        self.state = state
        self.telegram_handler = telegram_handler

        # Transferir configuraciones necesarias
        self.symbol = config.symbol
        self.tick_size = config.tick_size
        self.step_size = config.step_size
        self.take_profit_levels = config.take_profit_levels

    async def place_bracket_order(self, side, qty, entry_price_signal, sl_price, tp_prices, entry_type):
        """Coloca la orden de entrada (MARKET) y el bracket (SL/TP)."""
        try:
            mark_price_entry = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
            logging.info(f"Enviando MARKET {side} {qty} {self.symbol}")
            market = await self.client.futures_create_order(
                symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET, 
                quantity=format_qty(self.step_size, qty)
            )
        except BinanceAPIException as e:
            logging.error(f"Market order failed: {e}")
            await self.telegram_handler._send_message(f"❌ <b>ERROR ENTRY</b>\n{e}")
            self.state.trade_cooldown_until = time.time() + 300
            return

        filled, attempts, order_id = False, 0, market.get("orderId")
        avg_price, executed_qty = 0.0, 0.0

        while attempts < 15:
            try:
                status = await self.client.futures_get_order(symbol=self.symbol, orderId=order_id)
                if status.get("status") == "FILLED":
                    filled, avg_price, executed_qty = True, float(status.get("avgPrice", 0)), abs(float(status.get("executedQty", 0)))
                    break
            except Exception: pass
            attempts += 1
            await asyncio.sleep(0.5 + attempts * 0.1)

        if not filled:
            logging.error("Market order not confirmed filled; cooldown set")
            await self.telegram_handler._send_message("❌ <b>ERROR CRÍTICO</b>\nMARKET no confirmado FILLED.")
            self.state.trade_cooldown_until = time.time() + 300
            return

        sl_order_id = None
        try:
            batch = []
            num_tps = min(len(tp_prices), self.take_profit_levels)
            if num_tps == 0: raise Exception("No TP prices")
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))

            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            if (side == SIDE_BUY and float(sl_price) >= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])) or \
               (side == SIDE_SELL and float(sl_price) <= float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])):
                raise Exception("SL already surpassed by market price (fail-safe).")

            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "quantity": format_qty(self.step_size, executed_qty), 
                "stopPrice": format_price(self.tick_size, sl_price),
                "reduceOnly": "true"
            })

            remaining = Decimal(str(executed_qty))
            for i, tp in enumerate(tp_prices[:num_tps]):
                qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                qty_str = format_qty(self.step_size, qty_dec)

                if i == num_tps - 1 and remaining > 0 and remaining < Decimal(str(self.step_size)):
                    continue

                remaining -= Decimal(qty_str)
                mark_price = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
                tp_f = float(tp)

                if (side == SIDE_BUY and tp_f <= mark_price) or (side == SIDE_SELL and tp_f >= mark_price):
                    batch.append({"symbol": self.symbol, "side": sl_side, "type": ORDER_TYPE_MARKET, "quantity": qty_str, "reduceOnly": "true"})
                else:
                    batch.append({"symbol": self.symbol, "side": sl_side, "type": TAKE_PROFIT_MARKET, "quantity": qty_str, "stopPrice": format_price(self.tick_size, tp_f), "reduceOnly": "true"})

            results = await self.client.futures_place_batch_order(batchOrders=batch)
            logging.info(f"SL/TP batch response: {results}")

            if results and len(results) > 0 and "orderId" in results[0]:
                sl_order_id = results[0]["orderId"]
                logging.info(f"SL Order ID guardado: {sl_order_id}")
            else:
                logging.error("No se pudo obtener el orderId del SL del batch response.")

        except Exception as e:
            logging.error(f"Fallo creando SL/TP: {e}")
            await self.telegram_handler._send_message(f"⚠️ <b>FAIL-SAFE</b>\nFallo SL/TP: {e}")
            await self.close_position_manual(reason="Fallo al crear SL/TP batch")
            return 

        # Actualizar el ESTADO
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": executed_qty, "entry_price": avg_price,
            "entry_type": entry_type, "mark_price_entry": mark_price_entry,
            "atr_at_entry": self.state.cached_atr, "tps_hit_count": 0,
            "entry_time": time.time(), "sl_order_id": sl_order_id,
            "total_pnl": 0.0, "mark_price": mark_price_entry,
            "unrealized_pnl": 0.0,
        }
        self.state.last_known_position_qty = executed_qty
        self.state.sl_moved_to_be = False
        self.state.trade_cooldown_until = time.time() + 300
        self.state.save_state()

        # Enviar mensaje de Telegram
        icon = "🔼" if side == SIDE_BUY else "🔽"
        tp_list_str = ", ".join([format_price(self.tick_size, tp) for tp in tp_prices])
        msg = f"{icon} <b>NUEVA ORDEN: {entry_type}</b> {icon}\n\n" \
              f"<b>Símbolo</b>: <code>{self.symbol}</code>\n" \
              f"<b>Lado</b>: <code>{side}</code>\n" \
              f"<b>Cantidad</b>: <code>{format_qty(self.step_size, executed_qty)}</code>\n" \
              f"<b>Entrada</b>: <code>{format_price(self.tick_size, avg_price)}</code>\n" \
              f"<b>SL</b>: <code>{format_price(self.tick_size, sl_price)}</code> (ID: {sl_order_id})\n" \
              f"<b>TPs</b>: <code>{tp_list_str}</code>\n" \
              f"<b>ATR en Entrada</b>: <code>{self.state.cached_atr:.2f if self.state.cached_atr else 'N/A'}</code>\n"
        await self.telegram_handler._send_message(msg)

    async def move_sl_to_be(self, remaining_qty_float):
        """Mueve el SL a Breakeven (después del TP2)."""
        if self.state.sl_moved_to_be: return
        logging.info("Moviendo SL a Break-Even (disparado por TP2)...")
        try:
            entry_price = self.state.current_position_info.get("entry_price")
            side = self.state.current_position_info.get("side")
            old_sl_id = self.state.current_position_info.get("sl_order_id")

            if not entry_price or not side:
                logging.warning("No se puede mover SL a BE, falta info de entrada.")
                return

            if old_sl_id:
                try:
                    await self.client.futures_cancel_order(symbol=self.symbol, orderId=old_sl_id)
                    logging.info(f"Antiguo SL (ID: {old_sl_id}) cancelado.")
                except BinanceAPIException as e:
                    if e.code == -2011: logging.warning("SL antiguo ya no existía.")
                    else: raise e
            else:
                logging.warning("No se encontró old_sl_id para cancelar.")

            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            new_sl_order = await self.client.futures_create_order(
                symbol=self.symbol, side=sl_side, type=STOP_MARKET,
                quantity=format_qty(self.step_size, remaining_qty_float),
                stopPrice=format_price(self.tick_size, entry_price),
                reduceOnly="true"
            )

            new_sl_id = new_sl_order.get("orderId")
            self.state.sl_moved_to_be = True
            self.state.current_position_info["sl_order_id"] = new_sl_id
            self.state.save_state()
            await self.telegram_handler._send_message(f"🛡️ <b>TP2 ALCANZADO</b>\nSL movido a BE: <code>{format_price(self.tick_size, entry_price)}</code> (ID: {new_sl_id})")

        except Exception as e:
            logging.error(f"Error moviendo SL a BE: {e}")
            await self.telegram_handler._send_message("⚠️ Error al mover SL a Break-Even.")

    async def close_position_manual(self, reason="Manual Close"):
        """Cierra la posición actual a precio de mercado."""
        logging.warning(f"Cerrando posición manualmente: {reason}")
        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.symbol)

            # Usamos _get_current_position en lugar de la variable de estado
            # para asegurar que tenemos la cantidad MÁS reciente.
            pos = await self.client.futures_position_information()
            pos = next((p for p in pos if p["symbol"] == self.symbol), None)
            qty = float(pos.get("positionAmt", 0))

            if qty == 0:
                logging.info("Intento de cierre manual, pero la posición ya es 0.")
                if self.state.is_in_position: # Limpiar estado si estaba desincronizado
                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.last_known_position_qty = 0.0
                    self.state.sl_moved_to_be = False
                    self.state.save_state()
                return

            close_side = SIDE_SELL if qty > 0 else SIDE_BUY
            await self.client.futures_create_order(
                symbol=self.symbol, side=close_side, type=ORDER_TYPE_MARKET,
                quantity=format_qty(self.step_size, abs(qty)),
                reduceOnly="true"
            )
            logging.info(f"Orden MARKET de cierre enviada. Razón: {reason}")
        except Exception as e:
            logging.error(f"Error en _close_position_manual: {e}")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR</b>\nFallo al intentar cierre manual ({reason}).")
