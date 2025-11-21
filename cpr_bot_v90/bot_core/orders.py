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
        """Coloca entrada + SL/TP con validación de tamaño mínimo."""
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
        
        # --- VERIFICACIÓN DE LLENADO ---
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
            # Plan B: Verificar balance
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
            logging.error(f"[{self.symbol}] CRÍTICO: Orden enviada pero no confirmada.")
            await self.telegram_handler._send_message(f"🚨 <b>ERROR CRÍTICO ({self.symbol})</b>\nOrden enviada pero no confirmada.")
            self.state.trade_cooldown_until = time.time() + 300
            return

        # --- COLOCACIÓN DE SL / TP (CON FIX MIN NOTIONAL) ---
        sl_order_id = None
        try:
            batch = []
            
            # 1. Validar Tamaño para División
            notional_total = executed_qty * avg_price
            min_notional_binance = 6.0 # 5 es el límite, usamos 6 por seguridad
            
            # Si dividimos y queda muy chico, forzamos 1 solo TP
            target_tps = self.take_profit_levels
            if (notional_total / target_tps) < min_notional_binance:
                logging.warning(f"[{self.symbol}] Posición pequeña ({notional_total:.1f} USD). Forzando 1 TP único.")
                target_tps = 1
            
            num_tps = min(len(tp_prices), target_tps)
            tp_qty_per = Decimal(str(executed_qty)) / Decimal(str(num_tps))
            
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            
            # SL
            batch.append({
                "symbol": self.symbol, "side": sl_side, "type": STOP_MARKET,
                "quantity": format_qty(self.step_size, executed_qty), 
                "stopPrice": format_price(self.tick_size, sl_price),
                "reduceOnly": "true"
            })
            
            # TPs
            remaining = Decimal(str(executed_qty))
            final_tp_prices_used = []
            
            for i, tp in enumerate(tp_prices[:num_tps]):
                # Si forzamos 1 TP en una estrategia de rango (que tiene 3),
                # tomamos el TP1 (el primero de la lista) para asegurar ganancia rápida.
                # OJO: Si prefieres el TP2 (promedio), podrías cambiar la lógica, 
                # pero TP1 es más seguro para cuentas chicas.
                
                qty_dec = tp_qty_per if i < num_tps - 1 else remaining
                qty_str = format_qty(self.step_size, qty_dec)
                
                if i == num_tps - 1 and remaining > 0 and remaining < Decimal(str(self.step_size)): continue
                remaining -= Decimal(qty_str)
                
                final_tp_prices_used.append(tp)
                
                batch.append({
                    "symbol": self.symbol, "side": sl_side, "type": TAKE_PROFIT_MARKET, 
                    "quantity": qty_str, "stopPrice": format_price(self.tick_size, tp), 
                    "reduceOnly": "true"
                })
            
            results = await self.client.futures_place_batch_order(batchOrders=batch)
            if results and len(results) > 0 and "orderId" in results[0]:
                sl_order_id = results[0]["orderId"]
                logging.info(f"[{self.symbol}] SL/TP colocados correctamente.")

        except Exception as e:
            logging.error(f"[{self.symbol}] Fallo creando SL/TP: {e}")
            await self.telegram_handler._send_message(f"⚠️ <b>FAIL-SAFE ({self.symbol})</b>\nFallo al poner SL/TP. Cerrando posición por seguridad.")
            await self.close_position_manual(reason="Fallo SL/TP")
            return 

        # --- ACTUALIZAR ESTADO ---
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

        # --- NOTIFICACIÓN TELEGRAM ---
        try:
            atr_text = f"{self.state.cached_atr:.2f}" if self.state.cached_atr else "N/A"
            notional_usdt = executed_qty * avg_price
            side_icon = "🟢" if side == SIDE_BUY else "🔴"
            type_icon = "🚀" if "Breakout" in entry_type else "〰️"
            
            tp_list_str = "\n".join([f" {i+1}) {format_price(self.tick_size, tp)}" for i, tp in enumerate(final_tp_prices_used)])
            
            msg = (
                f"{type_icon} <b>NUEVA ORDEN: {self.symbol}</b>\n"
                f"────────────────\n"
                f"<b>Estrategia:</b> {entry_type} {side_icon}\n"
                f"📍 <b>Entrada:</b> <code>{format_price(self.tick_size, avg_price)}</code>\n"
                f"⚖️ <b>Cantidad:</b> <code>{format_qty(self.step_size, executed_qty)}</code>\n"
                f"💵 <b>Valor:</b> <code>~{notional_usdt:.1f} USDT</code>\n\n"
                f"🎯 <b>Objetivos:</b>\n{tp_list_str}\n\n"
                f"🛡️ <b>SL:</b> <code>{format_price(self.tick_size, sl_price)}</code>\n"
                f"📉 <b>ATR:</b> <code>{atr_text}</code>"
            )
            await self.telegram_handler._send_message(msg)
        except Exception as e:
            logging.error(f"Error enviando mensaje Telegram: {e}")
            try:
                simple = f"🚀 ORDEN {self.symbol} {side} @ {avg_price}"
                await self.telegram_handler._send_message(simple)
            except: pass

    # ... (Mantener move_sl_to_be, update_sl y close_position_manual IGUAL que antes) ...
    # COPIAR EL RESTO DE LAS FUNCIONES DE LA VERSIÓN ANTERIOR (v90.5)
    async def move_sl_to_be(self, remaining_qty_float):
        if self.state.sl_moved_to_be: return
        entry_price = self.state.current_position_info.get("entry_price")
        if not entry_price: return
        await self.update_sl(entry_price, remaining_qty_float, "Break-Even")
        self.state.sl_moved_to_be = True
        self.state.save_state()

    async def update_sl(self, new_price, qty, reason="Trailing"):
        old_sl_id = self.state.current_position_info.get("sl_order_id")
        side = self.state.current_position_info.get("side")
        if not side: return
        if old_sl_id:
            try: await self.client.futures_cancel_order(symbol=self.symbol, orderId=old_sl_id)
            except Exception: pass
        try:
            sl_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
            new_order = await self.client.futures_create_order(
                symbol=self.symbol, side=sl_side, type=STOP_MARKET,
                quantity=format_qty(self.step_size, qty),
                stopPrice=format_price(self.tick_size, new_price),
                reduceOnly="true"
            )
            self.state.current_position_info["sl_order_id"] = new_order.get("orderId")
            self.state.save_state()
            if reason == "Break-Even":
                await self.telegram_handler._send_message(f"🛡️ <b>{self.symbol}</b> SL movido a BE: {format_price(self.tick_size, new_price)}")
        except Exception as e:
            logging.error(f"[{self.symbol}] Error actualizando SL: {e}")

    async def close_position_manual(self, reason="Manual Close"):
        logging.warning(f"[{self.symbol}] Cerrando posición manualmente: {reason}")
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
            if self.state.is_in_position:
                self.state.is_in_position = False
                self.state.save_state()
        except Exception as e:
            logging.error(f"Error cierre manual: {e}")