import asyncio
import logging
import httpx

class TelegramHandler:
    def __init__(self, bot_instance, token, chat_id):
        """
        Inicializa el handler de Telegram.

        :param bot_instance: La instancia principal del bot (para acceder a su estado y métodos).
        :param token: El token del bot de Telegram.
        :param chat_id: El chat_id autorizado.
        """
        self.bot = bot_instance  # Esta es la referencia al bot principal
        self.token = token
        self.chat_id = chat_id
        self.httpx_client = httpx.AsyncClient(timeout=10.0)
        self.offset = None
        self.running = True

    async def _send_message(self, text):
        """Función de ayuda para enviar mensajes."""
        if not self.token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            await self.httpx_client.post(url, json=payload)
        except Exception as e:
            logging.error(f"Error enviando Telegram: {e}")

    async def _get_updates(self):
        """Función de ayuda para obtener actualizaciones."""
        if not self.token:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": 1, "offset": self.offset}
        try:
            r = await self.httpx_client.get(url, params=params)
            j = r.json()
            if j.get("ok"):
                return j.get("result", [])
        except Exception as e:
            logging.error(f"Error en _get_updates de Telegram: {e}")
        return []

    async def start_polling(self):
        """El bucle principal de sondeo de Telegram."""
        logging.info("Telegram poll loop started")
        while self.running:
            try:
                updates = await self._get_updates()
                for u in updates:
                    self.offset = u["update_id"] + 1
                    if "message" in u:
                        # Pasamos el mensaje al handler
                        await self._handle_message(u["message"])
            except Exception as e:
                logging.error(f"Error en el bucle de sondeo de Telegram: {e}")

            await asyncio.sleep(2) # Sigue revisando cada 2s

    async def stop(self):
        """Detiene el bucle de sondeo y cierra el cliente httpx."""
        self.running = False
        if self.httpx_client:
            await self.httpx_client.aclose()
        logging.info("Telegram handler detenido.")

    async def _handle_message(self, msg):
        """
        Maneja un mensaje entrante.
        Aquí es donde se conectan los comandos (ej. /cerrar)
        a las funciones del bot principal (ej. self.bot.close_position())
        """
        try:
            text = msg.get("text", "")
            chat_id = str(msg["chat"]["id"])

            # Filtro de seguridad
            if self.chat_id and chat_id != str(self.chat_id):
                logging.info(f"Telegram message from non-authorized chat {chat_id} ignored")
                return

            # --- Lógica de Comandos ---
            if text.startswith("/status"):
                await self._send_message(self._status_text())

            elif text.startswith("/pivots"):
                await self._send_message(self._pivots_text())

            elif text.startswith("/pausar"):
                # Llama a la función del bot principal
                await self.bot.pause_trading()
                await self._send_message("⏸️ <b>Bot Pausado</b>\nEl bot no buscará nuevas entradas.")

            elif text.startswith("/resumir"):
                # Llama a la función del bot principal
                await self.bot.resume_trading()
                await self._send_message("▶️ <b>Bot Reanudado</b>\nEl bot vuelve a buscar entradas.")

            elif text.startswith("/cerrar"):
                if not self.bot.is_in_position:
                    await self._send_message("ℹ️ No hay ninguna posición abierta para cerrar.")
                else:
                    logging.warning("Cierre manual solicitado por Telegram.")
                    await self._send_message("‼️ <b>Cerrando Posición</b>\nEnviando orden MARKET de cierre...")
                    # Llama a la función del bot principal
                    await self.bot.close_position_manual(reason="Comando /cerrar de Telegram")

            elif text.startswith("/forzar_indicadores"):
                logging.info("Forzando actualización de indicadores...")
                await self._send_message("⚙️ Forzando actualización de ATR, EMA y VolMedian(USDT)...")
                # Llama a la función del bot principal (como tarea)
                asyncio.create_task(self.bot.update_indicators())

            elif text.startswith("/forzar_pivotes"):
                logging.info("Forzando recálculo de pivotes...")
                await self._send_message("📐 Forzando recálculo de Pivotes...")
                # Llama a la función del bot principal (como tarea)
                asyncio.create_task(self.bot.calculate_pivots())

            elif text.startswith("/limit"):
                await self._send_message(f"Límite de pérdida diaria: {self.bot.daily_loss_limit_pct}%")

            elif text.startswith("/kill") or text.startswith("/restart"):
                await self._send_message("🔌 Bot apagándose por comando..."); 
                # Llama a la función del bot principal
                await self.bot.shutdown()

            else:
                await self._send_message(
                    "<b>Comando no reconocido.</b>\n"
                    "<code>/status</code>, <code>/pivots</code>, <code>/pausar</code>, <code>/resumir</code>, <code>/cerrar</code>, "
                    "<code>/forzar_indicadores</code>, <code>/forzar_pivotes</code>, "
                    "<code>/limit</code>, <code>/restart</code>"
                )

        except Exception as e:
            logging.error(f"Error handling telegram message: {e}", exc_info=True)

    # --- Funciones de Formato de Texto ---
    # (Estas funciones ahora leen el estado desde self.bot)

    def _status_text(self):
        s = f"<b>🤖 CPR BOT Status (v70)</b>\n"
        s += "──────────────────────\n"

        estado_bot = "🟢 ACTIVO" if not self.bot.trading_paused else "⏸️ PAUSADO"
        s += f"<b>Estado del Bot</b>: <code>{estado_bot}</code>\n"
        s += f"<b>Símbolo</b>: <code>{self.bot.symbol}</code>\n"

        if self.bot.daily_pivots:
            cw = self.bot.daily_pivots.get("width", 0)
            is_ranging = self.bot.daily_pivots.get("is_ranging_day", True)
            day_type = "Breakout" if not is_ranging else "Rango"
            s += f"📅 Día: <b>{day_type}</b> (CPR: {cw:.2f}%)\n"
        else:
            s += "📅 Día: <code>Calculando...</code>\n"

        s += "🎯 Modo: <b>Híbrido (CPR + Camarilla)</b>\n\n"

        if not self.bot.is_in_position:
            s += "📉 Posición:\n• <i>Sin posición abierta</i>\n\n"
        else:
            pos = self.bot.current_position_info
            side = pos.get('side', 'N/A')
            icon = "🔼" if side == "BUY" else "🔽"
            pnl_live = pos.get('unrealized_pnl', 0.0)
            pnl_icon = "🟢" if pnl_live >= 0 else "🔴"

            s += f"📉 Posición: {icon} <b>{side}</b>\n"
            s += f"• Cantidad: <code>{pos.get('quantity', 0)}</code>\n"
            s += f"• Entry: <code>{pos.get('entry_price', 0)}</code>\n"
            s += f"• Mark: <code>{pos.get('mark_price', 0)}</code>\n"
            s += f"• PnL: {pnl_icon} <code>{pnl_live:+.2f} USDT</code>\n\n"

        s += "📈 Indicadores:\n"
        atr_text = f"{self.bot.cached_atr:.2f}" if self.bot.cached_atr is not None else "..."
        ema_text = f"{self.bot.cached_ema:.2f}" if self.bot.cached_ema is not None else "..."
        vol_text = f"{self.bot.cached_median_vol:.0f}" if self.bot.cached_median_vol is not None else "..."
        s += f"• ATR({self.bot.atr_period}): <code>{atr_text}</code>\n"
        s += f"• EMA({self.bot.ema_period}): <code>{ema_text}</code>\n"
        s += f"• VolMedian(1m): <code>{vol_text} USDT</code>\n\n"

        s += f"⚠ Límite diario: {self.bot.daily_loss_limit_pct}%\n"

        total_closed_pnl = sum(t.get("pnl", 0) for t in self.bot.daily_trade_stats)
        unrealized_pnl = self.bot.current_position_info.get('unrealized_pnl', 0.0) if self.bot.is_in_position else 0.0
        total_daily_pnl = total_closed_pnl + unrealized_pnl
        pnl_pct_str = "..."

        if self.bot.daily_start_balance and self.bot.daily_start_balance > 0:
            pnl_pct = (total_daily_pnl / self.bot.daily_start_balance) * 100
            pnl_pct_str = f"{pnl_pct:+.2f}%"

        s += f"• PnL diario: <code>{total_daily_pnl:+.2f} USDT ({pnl_pct_str})</code>\n"
        s += "──────────────────────"

        return s

    def _pivots_text(self):
        if not self.bot.daily_pivots:
            return "📐 Pivotes no calculados aún."

        p = self.bot.daily_pivots
        s = f"📊 <b>Pivotes Camarilla ({self.bot.symbol})</b>\n\n"
        s += f"H: <code>{p.get('Y_H', 0.0):.1f}</code>\n"
        s += f"L: <code>{p.get('Y_L', 0.0):.1f}</code>\n"
        s += f"C: <code>{p.get('Y_C', 0.0):.1f}</code>\n\n"

        s += f"🔥 <b>R6</b>: <code>{p.get('H6', 0.0):.2f}</code>\n"
        s += f"🔴 <b>R5</b>: <code>{p.get('H5', 0.0):.2f}</code>\n"
        s += f"🔴 R4: <code>{p.get('H4', 0.0):.2f}</code>\n"
        s += f"🔴 R3: <code>{p.get('H3', 0.0):.2f}</code>\n"
        s += f"🟡 R2: <code>{p.get('H2', 0.0):.2f}</code>\n"
        s += f"🟡 R1: <code>{p.get('H1', 0.0):.2f}</code>\n\n"

        s += f"🟢 S1: <code>{p.get('L1', 0.0):.2f}</code>\n"
        s += f"🟢 S2: <code>{p.get('L2', 0.0):.2f}</code>\n"
        s += f"🟢 S3: <code>{p.get('L3', 0.0):.2f}</code>\n"
        s += f"🔵 S4: <code>{p.get('L4', 0.0):.2f}</code>\n"
        s += f"🔵 S5: <code>{p.get('L5', 0.0):.2f}</code>\n"
        s += f"🔵 <b>S6</b>: <code>{p.get('L6', 0.0):.2f}</code>\n"

        return s
