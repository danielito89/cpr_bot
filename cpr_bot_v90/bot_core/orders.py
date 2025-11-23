import logging
import time
import asyncio
from decimal import Decimal
from binance.exceptions import BinanceAPIException

from .utils import (
    format_price, format_qty,
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, 
    STOP_MARKET, TAKE_PROFIT_MARKET
)

class OrdersManager:
    def __init__(self, client, state, telegram_handler, config):
        self.client = client
        self.state = state
        self.telegram_handler = telegram_handler
        self.symbol = config.symbol
        self.tick_size = config.tick_size
        self.step_size = config.step_size
        self.take_profit_levels = config.take_profit_levels

    async def place_bracket_order(self, side, qty, entry_price_signal, sl_price, tp_prices, entry_type):
        # ... (El código de entrada se mantiene igual, lo incluyo completo abajo) ...
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
        
        while attempts < 10:
            try:
                status = await self.client.futures_get_order(symbol=self.symbol, orderId=order_id)
                if status.get("status") == "FILLED":
                    filled = True
                    avg_price = float(status.get("avgPrice", 0))
                    executed_qty = abs(float(status.get("executedQty", 0)))
                    break
            except Exception: pass
            attempts += 1
            await asyncio.sleep(0.5)
        
        if not filled:
            # Plan B
            logging.warning(f"[{self.symbol}] Orden no confirmada por ID. Verificando posición...")
            try:
                pos = await self.client.futures_position_information()
                my_pos = next((p for p in pos if p["symbol"] == self.symbol), None)
                if my_pos:
                    pos_amt = abs(float(my_pos.get("positionAmt", 0)))
                    if pos_amt > 0 and abs(pos_amt - qty) < (qty * 0.1): 
                        logging.info(f"[{self.symbol}] ¡Posición confirmada por balance! Procediendo.")
                        filled = True
                        avg_price = float(my_pos.get("entryPrice", entry_price_signal))
                        executed_qty = pos_amt
            except Exception as e:
                logging.error(f"Fallo en verificación Plan B: {e}")

        if not filled:
            logging.error(f"[{self.symbol}] CRÍTICO: Orden no confirmada.")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR CRÍTICO ({self.symbol})</b>\nOrden enviada pero no confirmada.")
            self.state.trade_cooldown_until = time.time() + 300
            return

        # SL / TP con Fix Min Notional
        sl_order_id = None
        try:
            batch = []
            notional_total = executed_qty * avg_price
            min_notional = 6.0
            
            target_tps = self.take_profit_levels
            if (notional_total / target_tps) < min_notional:
                logging.warning(f"[{self.symbol}] Posición chica ({notional_total:.1f}). 1 TP único.")
                target_tps = 1
            
            num_tps = min(len(tp_prices), target_tps)
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            
            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "quantity": format_qty(self.step_size, executed_qty), 
                "stopPrice": format_price(self.tick_size, sl_price),
                "reduceOnly": "true"
            })
            
            remaining = Decimal(str(executed_qty))
            final_tps = []
            for i, tp in enumerate(tp_prices[:num_tps]):
                qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                qty_str = format_qty(self.step_size, qty_dec)
                if i == num_tps - 1 and remaining > 0 and remaining < Decimal(str(self.step_size)): continue
                remaining -= Decimal(qty_str)
                final_tps.append(tp)
                
                batch.append({
                    "symbol": self.symbol, "side": sl_side, "type": TAKE_PROFIT_MARKET, 
                    "quantity": qty_str, "stopPrice": format_price(self.tick_size, tp), 
                    "reduceOnly": "true"
                })
            
            results = await self.client.futures_place_batch_order(batchOrders=batch)
            if results and len(results) > 0 and "orderId" in results[0]:
                sl_order_id = results[0]["orderId"]

        except Exception as e:
            logging.error(f"[{self.symbol}] Fallo SL/TP: {e}")
            await self.telegram_handler._send_message(f"⚠️ <b>FAIL-SAFE ({self.symbol})</b>\nFallo SL/TP. Cerrando.")
            await self.close_position_manual(reason="Fallo SL/TP")
            return 

        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": executed_qty, "entry_price": avg_price,
            "entry_type": entry_type, "mark_price_entry": avg_price,
            "atr_at_entry": self.state.cached_atr, "tps_hit_count": 0,
            "entry_time": time.time(), "sl_order_id": sl_order_id,
            "total_pnl": 0.0
        }
        self.state.last_known_position_qty = executed_qty
        self.state.sl_moved_to_be = False
        self.state.trade_cooldown_until = time.time() + 300
        self.state.save_state()

        try:
            atr_text = f"{self.state.cached_atr:.2f}" if self.state.cached_atr else "N/A"
            notional = executed_qty * avg_price
            icon = "🟢" if side == SIDE_BUY else "🔴"
            tp_str = "\n".join([f" {i+1}) {format_price(self.tick_size, t)}" for i, t in enumerate(final_tps)])
            
            msg = (f"{icon} <b>NUEVA ORDEN: {self.symbol}</b>\n"
                   f"<b>Tipo:</b> {entry_type}\n"
                   f"<b>Entrada:</b> {format_price(self.tick_size, avg_price)}\n"
                   f"<b>Valor:</b> ~{notional:.1f} USDT\n\n"
                   f"🎯 <b>TPs:</b>\n{tp_str}\n\n"
                   f"🛡️ <b>SL:</b> {format_price(self.tick_size, sl_price)}\n"
                   f"📉 <b>ATR:</b> {atr_text}")
            await self.telegram_handler._send_message(msg)
        except Exception: pass

    async def move_sl_to_be(self, remaining_qty_float):
        if self.state.sl_moved_to_be: return
        entry = self.state.current_position_info.get("entry_price")
        if entry:
            await self.update_sl(entry, remaining_qty_float, "Break-Even")
            self.state.sl_moved_to_be = True
            self.state.save_state()

    async def update_sl(self, new_price, qty, reason="Trailing"):
        old_id = self.state.current_position_info.get("sl_order_id")
        side = self.state.current_position_info.get("side")
        if not side: return

        if old_id:
            try: await self.client.futures_cancel_order(symbol=self.symbol, orderId=old_id)
            except Exception: pass

        try:
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            res = await self.client.futures_create_order(
                symbol=self.symbol, side=sl_side, type=STOP_MARKET,
                quantity=format_qty(self.step_size, qty),
                stopPrice=format_price(self.tick_size, new_price),
                reduceOnly="true"
            )
            self.state.current_position_info["sl_order_id"] = res.get("orderId")
            self.state.save_state()
            
            if reason == "Break-Even":
                await self.telegram_handler._send_message(f"🛡️ <b>{self.symbol}</b> SL a BE: {format_price(self.tick_size, new_price)}")
        except Exception as e:
            logging.error(f"Error update SL: {e}")

    # --- FIX ROBUSTO: CIERRE MANUAL BLINDADO ---
    async def close_position_manual(self, reason="Manual Close"):
        logging.warning(f"[{self.symbol}] Intentando cierre manual: {reason}")
        
        # PASO 1: Cancelar Órdenes (Si falla, seguimos igual)
        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.symbol)
            logging.info(f"[{self.symbol}] Órdenes pendientes canceladas.")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error cancelando órdenes: {e}")

        # PASO 2: Cerrar Posición (Si hay)
        try:
            pos = await self.client.futures_position_information()
            p = next((x for x in pos if x["symbol"] == self.symbol), None)
            if p:
                qty = float(p.get("positionAmt", 0))
                if qty != 0:
                    logging.info(f"[{self.symbol}] Cerrando qty {qty} a mercado...")
                    side = SIDE_SELL if qty > 0 else SIDE_BUY
                    await self.client.futures_create_order(
                        symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET,
                        quantity=format_qty(self.step_size, abs(qty)), reduceOnly="true"
                    )
                else:
                    logging.info(f"[{self.symbol}] Posición ya es 0 en Binance.")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error enviando orden de cierre: {e}")
            # Si falla el cierre, NO reseteamos estado (para que el zombie killer lo intente de nuevo)
            # return 
            # OJO: Si la orden falla, quizás ya no tengamos posición.
            pass

        # PASO 3: Verificar Final y Resetear Estado
        # Hacemos una última verificación. Si qty es 0 (o muy cerca), limpiamos estado.
        try:
            # Esperar un momento para que se procese
            await asyncio.sleep(1)
            pos_final = await self.client.futures_position_information()
            p_final = next((x for x in pos_final if x["symbol"] == self.symbol), None)
            
            qty_final = abs(float(p_final.get("positionAmt", 0))) if p_final else 0.0
            
            # Si ya está cerrado (o es polvo), liberamos al bot
            if qty_final < 0.0001:
                if self.state.is_in_position:
                    logging.info(f"[{self.symbol}] Confirmado cerrado. Reseteando estado local.")
                    self.state.is_in_position = False
                    self.state.current_position_info = {}
                    self.state.sl_moved_to_be = False
                    self.state.save_state()
            else:
                logging.warning(f"[{self.symbol}] ALERTA: La posición sigue abierta ({qty_final}) tras intento de cierre.")
                await self.telegram_handler._send_message(f"🚨 <b>{self.symbol}</b>: Falló el cierre automático. Cerrar manual.")
                
        except Exception as e:
            logging.error(f"Error verificando cierre: {e}")