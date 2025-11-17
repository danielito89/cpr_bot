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
            logging.info(f"[{self.symbol}] Enviando MARKET {side} {qty}")
            market = await self.client.futures_create_order(
                symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET, 
                quantity=format_qty(self.step_size, qty)
            )
        except BinanceAPIException as e:
            logging.error(f"[{self.symbol}] Market order failed: {e}")
            await self.telegram_handler._send_message(f"❌ <b>ERROR ENTRY ({self.symbol})</b>\n{e}")
            self.state.trade_cooldown_until = time.time() + 300
            return
        
        filled, attempts, order_id = False, 0, market.get("orderId")
        avg_price, executed_qty = 0.0, 0.0
        
        # Polling rápido para confirmar fill
        while attempts < 15:
            try:
                status = await self.client.futures_get_order(symbol=self.symbol, orderId=order_id)
                if status.get("status") == "FILLED":
                    filled = True
                    avg_price = float(status.get("avgPrice", 0))
                    executed_qty = abs(float(status.get("executedQty", 0)))
                    break
            except Exception: pass
            attempts += 1
            await asyncio.sleep(0.5 + attempts * 0.1)
        
        if not filled:
            logging.error(f"[{self.symbol}] Market order not confirmed filled; cooldown set")
            await self.telegram_handler._send_message(f"❌ <b>ERROR CRÍTICO ({self.symbol})</b>\nMARKET no confirmado FILLED.")
            self.state.trade_cooldown_until = time.time() + 300
            return

        sl_order_id = None
        try:
            batch = []
            num_tps = min(len(tp_prices), self.take_profit_levels)
            if num_tps == 0: raise Exception("No TP prices")
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))
            
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            
            # Orden de Stop Loss
            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "quantity": format_qty(self.step_size, executed_qty), 
                "stopPrice": format_price(self.tick_size, sl_price),
                "reduceOnly": "true"
            })
            
            # Órdenes de Take Profit
            remaining = Decimal(str(executed_qty))
            for i, tp in enumerate(tp_prices[:num_tps]):
                qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                qty_str = format_qty(self.step_size, qty_dec)
                
                if i == num_tps - 1 and remaining > 0 and remaining < Decimal(str(self.step_size)):
                    continue
                
                remaining -= Decimal(qty_str)
                mark_price = float((await self.client.futures_mark_price(symbol=self.symbol))["markPrice"])
                tp_f = float(tp)
                
                # Si el precio ya cruzó el TP, ejecutar a mercado
                if (side == SIDE_BUY and tp_f <= mark_price) or (side == SIDE_SELL and tp_f >= mark_price):
                    batch.append({"symbol": self.symbol, "side": sl_side, "type": ORDER_TYPE_MARKET, "quantity": qty_str, "reduceOnly": "true"})
                else:
                    batch.append({"symbol": self.symbol, "side": sl_side, "type": TAKE_PROFIT_MARKET, "quantity": qty_str, "stopPrice": format_price(self.tick_size, tp_f), "reduceOnly": "true"})
            
            results = await self.client.futures_place_batch_order(batchOrders=batch)
            logging.info(f"[{self.symbol}] SL/TP batch response: {results}")
            
            if results and len(results) > 0 and "orderId" in results[0]:
                sl_order_id = results[0]["orderId"]
                logging.info(f"[{self.symbol}] SL Order ID guardado: {sl_order_id}")
            else:
                logging.error(f"[{self.symbol}] No se pudo obtener el orderId del SL del batch response.")

        except Exception as e:
            logging.error(f"[{self.symbol}] Fallo creando SL/TP: {e}")
            await self.telegram_handler._send_message(f"⚠️ <b>FAIL-SAFE ({self.symbol})</b>\nFallo SL/TP: {e}")
            await self.close_position_manual(reason="Fallo al crear SL/TP batch")
            return 

        # Actualizar el ESTADO
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": executed_qty, "entry_price": avg_price,
            "entry_type": entry_type, "mark_price_entry": avg_price,
            "atr_at_entry": self.state.cached_atr, "tps_hit_count": 0,
            "entry_time": time.time(), "sl_order_id": sl_order_id,
            "total_pnl": 0.0, "mark_price": avg_price,
            "unrealized_pnl": 0.0,
        }
        self.state.last_known_position_qty = executed_qty
        self.state.sl_moved_to_be = False
        self.state.trade_cooldown_until = time.time() + 300
        self.state.save_state()

        # --- FORMATO DE MENSAJE MEJORADO ---
        try:
            notional_usdt = executed_qty * avg_price
            
            side_icon = "🟢" if side == SIDE_BUY else "🔴"
            type_icon = "🚀" if "Breakout" in entry_type else "〰️"
            
            msg = f"{type_icon} <b>NUEVA ORDEN: {self.symbol}</b>\n"
            msg += "──────────────────────\n"
            msg += f"<b>Estrategia:</b> {entry_type} {side_icon}\n\n"
            
            msg += f"📍 <b>Entrada:</b> <code>{format_price(self.tick_size, avg_price)}</code>\n"
            msg += f"⚖️ <b>Cantidad:</b> <code>{format_qty(self.step_size, executed_qty)}</code>\n"
            msg += f"💵 <b>Valor:</b> <code>~{notional_usdt:.2f} USDT</code>\n\n"
            
            msg += "🎯 <b>Objetivos (TPs):</b>\n"
            for i, tp in enumerate(tp_prices):
                 msg += f" {i+1}) <code>{format_price(self.tick_size, tp)}</code>\n"
            
            msg += f"\n🛡️ <b>Stop Loss:</b> <code>{format_price(self.tick_size, sl_price)}</code>\n"
            msg += "──────────────────────"
            
            await self.telegram_handler._send_message(msg)
        except Exception as e:
            logging.error(f"[{self.symbol}] Error enviando mensaje de nueva orden: {e}")


    async def move_sl_to_be(self, remaining_qty_float):
        """Mueve el SL a Breakeven (después del TP2)."""
        if self.state.sl_moved_to_be: return
        
        logging.info(f"[{self.symbol}] Moviendo SL a Break-Even (disparado por TP2)...")
        try:
            entry_price = self.state.current_position_info.get("entry_price")
            side = self.state.current_position_info.get("side")
            old_sl_id = self.state.current_position_info.get("sl_order_id")
            
            if not entry_price or not side:
                logging.warning(f"[{self.symbol}] Falta info para mover SL a BE.")
                return

            if old_sl_id:
                try:
                    await self.client.futures_cancel_order(symbol=self.symbol, orderId=old_sl_id)
                    logging.info(f"[{self.symbol}] Antiguo SL (ID: {old_sl_id}) cancelado.")
                except BinanceAPIException as e:
                    if e.code == -2011: logging.warning(f"[{self.symbol}] SL antiguo ya no existía.")
                    else: raise e
            else:
                logging.warning(f"[{self.symbol}] No se encontró old_sl_id para cancelar.")

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
            await self.telegram_handler._send_message(f"🛡️ <b>{self.symbol} TP2 ALCANZADO</b>\nSL movido a BE: <code>{format_price(self.tick_size, entry_price)}</code>")

        except Exception as e:
            logging.error(f"[{self.symbol}] Error moviendo SL a BE: {e}")
            await self.telegram_handler._send_message(f"⚠️ Error al mover SL a Break-Even en {self.symbol}.")

    async def close_position_manual(self, reason="Manual Close"):
        """Cierra la posición actual a precio de mercado."""
        logging.warning(f"[{self.symbol}] Cerrando posición manualmente: {reason}")
        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.symbol)
            
            pos = await self.client.futures_position_information()
            pos = next((p for p in pos if p["symbol"] == self.symbol), None)
            qty = float(pos.get("positionAmt", 0))
            
            if qty == 0:
                logging.info(f"[{self.symbol}] Intento de cierre manual, pero la posición ya es 0.")
                if self.state.is_in_position:
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
            logging.info(f"[{self.symbol}] Orden MARKET de cierre enviada. Razón: {reason}")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error en _close_position_manual: {e}")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR ({self.symbol})</b>\nFallo al intentar cierre manual ({reason}).")
