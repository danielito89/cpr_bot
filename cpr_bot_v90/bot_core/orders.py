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
        
        # Verificar llenado
        filled = False
        order_id = market.get("orderId")
        avg_price = 0.0
        executed_qty = 0.0
        
        attempts = 0
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
            try:
                pos = await self.client.futures_position_information()
                my_pos = next((p for p in pos if p["symbol"] == self.symbol), None)
                if my_pos:
                    pos_amt = abs(float(my_pos.get("positionAmt", 0)))
                    if pos_amt > 0 and abs(pos_amt - qty) < (qty * 0.1): 
                        filled = True
                        avg_price = float(my_pos.get("entryPrice", entry_price_signal))
                        executed_qty = pos_amt
            except Exception: pass

        if not filled:
            logging.error(f"[{self.symbol}] CRÍTICO: Orden no confirmada.")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR CRÍTICO ({self.symbol})</b>\nOrden enviada pero no confirmada.")
            self.state.trade_cooldown_until = time.time() + 300
            return

        # --- SL / TP ---
        sl_order_id = None
        try:
            batch = []
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            
            # 1. STOP LOSS "NUCLEAR"
            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "stopPrice": format_price(self.tick_size, sl_price),
                "closePosition": "true"
            })
            
            # 2. TAKE PROFITS
            notional_total = executed_qty * avg_price
            target_tps = self.take_profit_levels
            if (notional_total / target_tps) < 6.0: target_tps = 1
            
            num_tps = min(len(tp_prices), target_tps)
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))
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
            
            # --- VALIDACIÓN ESTRICTA (FIX v96) ---
            # Si results está vacío o el primer elemento (SL) no tiene orderId, FALLAMOS.
            if not results or len(results) == 0:
                raise Exception(f"Batch order devolvió respuesta vacía: {results}")
            
            # Verificar explícitamente el SL (índice 0)
            sl_result = results[0]
            if "orderId" not in sl_result:
                # Si hay un código de error, lo levantamos para activar el cierre de emergencia
                err_msg = sl_result.get("msg", "Unknown Error")
                raise Exception(f"SL rechazado por Binance: {err_msg}")
            
            sl_order_id = sl_result["orderId"]
            logging.info(f"[{self.symbol}] SL confirmado (ID: {sl_order_id}).")
            # -------------------------------------

        except Exception as e:
            logging.error(f"[{self.symbol}] FALLO CRÍTICO SL/TP: {e}")
            await self.telegram_handler._send_message(f"⚠️ <b>FAIL-SAFE ({self.symbol})</b>\nSL rechazado. CERRANDO POSICIÓN YA.")
            # ¡Cierre de emergencia inmediato!
            await self.close_position_manual(reason="Fallo SL/TP")
            return 

        # Actualizar Estado
        self.state.is_in_position = True
        self.state.current_position_info = {
            "side": side, "quantity": executed_qty, "entry_price": avg_price,
            "entry_type": entry_type, "mark_price_entry": avg_price,
            "atr_at_entry": self.state.cached_atr, "tps_hit_count": 0,
            "entry_time": time.time(), "sl_order_id": sl_order_id,
            "total_pnl": 0.0, "unrealized_pnl": 0.0
        }
        self.state.last_known_position_qty = executed_qty
        self.state.sl_moved_to_be = False
        self.state.trade_cooldown_until = time.time() + 300
        self.state.save_state()

        # Notificar
        try:
            atr_val = self.state.cached_atr
            atr_text = format_price(self.tick_size, atr_val) if atr_val else "N/A"
            notional_usdt = executed_qty * avg_price
            side_icon = "🟢" if side == SIDE_BUY else "🔴"
            tp_str = "\n".join([f" {i+1}) {format_price(self.tick_size, t)}" for i, t in enumerate(final_tps)])
            
            msg = (
                f"{side_icon} <b>NUEVA ORDEN: {self.symbol}</b>\n"
                f"<b>Tipo:</b> {entry_type}\n"
                f"<b>Entrada:</b> {format_price(self.tick_size, avg_price)}\n"
                f"<b>Valor:</b> ~{notional_usdt:.1f} USDT\n\n"
                f"🎯 <b>TPs:</b>\n{tp_str}\n\n"
                f"🛡️ <b>SL:</b> {format_price(self.tick_size, sl_price)}\n"
                f"📉 <b>ATR:</b> {atr_text}"
            )
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
            new_order = await self.client.futures_create_order(
                symbol=self.symbol, side=sl_side, type=STOP_MARKET,
                stopPrice=format_price(self.tick_size, new_price),
                closePosition="true"
            )
            self.state.current_position_info["sl_order_id"] = new_order.get("orderId")
            self.state.save_state()
            
            if reason == "Break-Even":
                await self.telegram_handler._send_message(f"🛡️ <b>{self.symbol}</b> SL a BE: {format_price(self.tick_size, new_price)}")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error actualizando SL: {e}")

    async def close_position_manual(self, reason="Manual Close"):
        logging.warning(f"[{self.symbol}] Cerrando manual: {reason}")
        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.symbol)
            pos = await self.client.futures_position_information()
            p = next((p for p in pos if p["symbol"] == self.symbol), None)
            if p:
                qty = float(p.get("positionAmt", 0))
                if qty != 0:
                    side = SIDE_SELL if qty > 0 else SIDE_BUY
                    await self.client.futures_create_order(
                        symbol=self.symbol, side=side, type=ORDER_TYPE_MARKET,
                        quantity=format_qty(self.step_size, abs(qty)), reduceOnly="true"
                    )
            
            self.state.is_in_position = False
            self.state.current_position_info = {}
            self.state.save_state()
        except Exception as e:
            logging.error(f"Error cierre manual: {e}")