import asyncio
import logging
import httpx

class TelegramHandler:
    def __init__(self, orchestrator, token, chat_id):
        """
        :param orchestrator: Referencia al BotOrchestrator (main_v90).
        """
        self.orchestrator = orchestrator
        self.token = token
        self.chat_id = chat_id
        self.httpx_client = httpx.AsyncClient(timeout=10.0)
        self.offset = None
        self.running = True

    async def _send_message(self, text):
        if not self.token or not self.chat_id: return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            await self.httpx_client.post(url, json=payload)
        except Exception as e:
            logging.error(f"Error enviando Telegram: {e}")

    async def _get_updates(self):
        if not self.token: return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        params = {"timeout": 1, "offset": self.offset}
        try:
            r = await self.httpx_client.get(url, params=params)
            j = r.json()
            if j.get("ok"): return j.get("result", [])
        except Exception as e:
            logging.error(f"Error en _get_updates: {e}")
        return []

    async def start_polling(self):
        logging.info("Telegram poll loop started")
        while self.running:
            try:
                updates = await self._get_updates()
                for u in updates:
                    self.offset = u["update_id"] + 1
                    if "message" in u:
                        await self._handle_message(u["message"])
            except Exception as e:
                logging.error(f"Telegram loop error: {e}")
            await asyncio.sleep(2)

    async def stop(self):
        self.running = False
        if self.httpx_client: await self.httpx_client.aclose()

    async def _handle_message(self, msg):
        try:
            text = msg.get("text", "").strip()
            chat_id = str(msg["chat"]["id"])
            
            if self.chat_id and chat_id != str(self.chat_id): return
            
            # Parsear comando y argumentos
            parts = text.split()
            cmd = parts[0].lower()
            arg = parts[1].upper() if len(parts) > 1 else None

            # --- COMANDOS ---

            if cmd == "/status":
                report = self._generate_multibot_status(target_symbol=arg)
                await self._send_message(report)

            elif cmd == "/pivots":
                if arg:
                    bot = self.orchestrator.strategies.get(arg)
                    if bot: await self._send_message(self._generate_pivots_text(bot))
                    else: await self._send_message(f"⚠️ No encuentro el bot {arg}")
                else:
                    for bot in self.orchestrator.strategies.values():
                        await self._send_message(self._generate_pivots_text(bot))
            
            elif cmd == "/start":
                if not arg:
                    await self._send_message("⚠️ Uso: <code>/start BTCUSDT</code>")
                else:
                    await self._send_message(f"⏳ Iniciando <b>{arg}</b>...")
                    success = await self.orchestrator.add_pair(arg)
                    if success: await self._send_message(f"✅ <b>{arg}</b> iniciado correctamente.")
                    else: await self._send_message(f"❌ Error al iniciar <b>{arg}</b> (¿Ya existe?).")

            elif cmd == "/stop":
                if not arg:
                    await self._send_message("⚠️ Uso: <code>/stop BTCUSDT</code>")
                else:
                    await self._send_message(f"⏳ Deteniendo <b>{arg}</b>...")
                    success = await self.orchestrator.remove_pair(arg)
                    if success: await self._send_message(f"🛑 <b>{arg}</b> detenido y memoria liberada.")
                    else: await self._send_message(f"⚠️ <b>{arg}</b> no estaba corriendo.")

            elif cmd == "/list":
                active = list(self.orchestrator.strategies.keys())
                await self._send_message(f"📋 <b>Bots Activos ({len(active)}):</b>\n" + ", ".join(active))

            elif cmd == "/pausar":
                target = arg if arg else "TODOS"
                await self.orchestrator.pause_all(target_symbol=arg)
                await self._send_message(f"⏸️ Trading pausado para: <b>{target}</b>")

            elif cmd == "/resumir":
                target = arg if arg else "TODOS"
                await self.orchestrator.resume_all(target_symbol=arg)
                await self._send_message(f"▶️ Trading reanudado para: <b>{target}</b>")

            elif cmd == "/cerrar":
                if not arg:
                    await self._send_message("⚠️ Seguridad: Debes especificar el par. Ej: <code>/cerrar BTCUSDT</code>")
                else:
                    bot = self.orchestrator.strategies.get(arg)
                    if bot:
                        await self._send_message(f"‼️ Cerrando posición en <b>{arg}</b>...")
                        await bot.close_position_manual(reason="Comando Telegram")
                    else:
                        await self._send_message(f"Bot {arg} no encontrado.")

            # --- NUEVO COMANDO /reset ---
            elif cmd == "/reset":
                if not arg:
                    await self._send_message("⚠️ Uso: <code>/reset BTCUSDT</code> (Solo usar si el bot se traba)")
                else:
                    bot = self.orchestrator.strategies.get(arg)
                    if bot:
                        await self._send_message(f"🔄 <b>Reseteando estado de {arg}...</b>")
                        await bot.force_reset_state()
                        await self._send_message(f"✅ <b>{arg}</b> reseteado. Listo para nuevas señales.")
                    else:
                        await self._send_message(f"Bot {arg} no encontrado.")

            elif cmd == "/limit":
                 await self._send_message(f"Límite de pérdida diaria: {self.orchestrator.DEFAULT_CONFIG['DAILY_LOSS_LIMIT_PCT']}%")
            
            elif cmd == "/restart":
                 await self._send_message("♻️ Reiniciando Orquestador...")
                 await self.orchestrator.shutdown()

            else:
                await self._send_message(
                    "<b>Comando no reconocido.</b>\n"
                    "Comandos disponibles:\n"
                    "<code>/status</code> - Ver estado general\n"
                    "<code>/pivots</code> - Ver pivotes del día\n"
                    "<code>/pausar</code> - Pausar nuevas entradas\n"
                    "<code>/resumir</code> - Reanudar nuevas entradas\n"
                    "<code>/cerrar</code> - Cerrar posición actual\n"
                    "<code>/forzar_indicadores</code> - Recalcular EMA/ATR/Vol\n"
                    "<code>/forzar_pivotes</code> - Recalcular Pivotes\n"
                    "<code>/limit</code> - Ver límite de pérdida\n"
                    "<code>/restart</code> - Reiniciar el bot"
                )
            
        except Exception as e:
            logging.error(f"Error handle message: {e}", exc_info=True)

    # --- Generadores de Texto ---

    def _generate_multibot_status(self, target_symbol=None):
        if not self.orchestrator.strategies:
            return "💤 No hay bots activos. Usa <code>/start BTCUSDT</code>"
        
        bots_to_show = []
        if target_symbol:
            bot = self.orchestrator.strategies.get(target_symbol)
            if bot: bots_to_show.append(bot)
        else:
            bots_to_show = list(self.orchestrator.strategies.values())

        if not bots_to_show: return f"No se encontró {target_symbol}"

        full_msg = ""
        for bot in bots_to_show:
            full_msg += self._generate_single_status(bot) + "\n\n"
        return full_msg

    def _generate_single_status(self, bot):
        s = f"<b>🤖 {bot.symbol}</b> "
        s += "⏸️ PAUSADO" if bot.state.trading_paused else "🟢 ACTIVO"
        s += "\n"
        
        day_type = "Calc..."
        if bot.state.daily_pivots:
            is_range = bot.state.daily_pivots.get("is_ranging_day", True)
            cw = bot.state.daily_pivots.get("width", 0)
            day_type = f"{'Rango' if is_range else 'Breakout'} (CPR {cw:.2f}%)"
        s += f"📅 {day_type}\n"

        if bot.state.is_in_position:
            pos = bot.state.current_position_info
            pnl = pos.get('unrealized_pnl', 0.0)
            icon = "🟢" if pnl >= 0 else "🔴"
            s += f"📉 <b>{pos.get('side')}</b> | PnL: {icon} {pnl:.2f} | Mark: {pos.get('mark_price')}\n"
        else:
            s += "Checking signals...\n"

        atr = f"{bot.state.cached_atr:.2f}" if bot.state.cached_atr else "-"
        vol = f"{bot.state.cached_median_vol/1000:.1f}k" if bot.state.cached_median_vol else "-"
        s += f"📈 ATR: {atr} | VolMed: {vol}"
        
        return s

    def _generate_pivots_text(self, bot):
        """Genera el mensaje detallado de pivotes para el usuario."""
        p = bot.state.daily_pivots
        if not p: return f"<b>{bot.symbol}</b>: Sin pivotes."
        
        s = f"📊 <b>Pivotes Camarilla ({bot.symbol})</b>\n\n"
        s += f"H: <code>{p.get('Y_H', 0.0):.2f}</code>\n"
        s += f"L: <code>{p.get('Y_L', 0.0):.2f}</code>\n"
        s += f"C: <code>{p.get('Y_C', 0.0):.2f}</code>\n\n"
        
        s += f"🔥 <b>R6 (Target):</b> <code>{p.get('H6', 0.0):.2f}</code>\n"
        s += f"🔴 <b>R5 (Target):</b> <code>{p.get('H5', 0.0):.2f}</code>\n"
        s += f"🔴 R4 (Breakout): <code>{p.get('H4', 0.0):.2f}</code>\n"
        s += f"🔴 R3 (Rango): <code>{p.get('H3', 0.0):.2f}</code>\n"
        s += f"🟡 R2: <code>{p.get('H2', 0.0):.2f}</code>\n"
        s += f"🟡 R1: <code>{p.get('H1', 0.0):.2f}</code>\n\n"
        
        s += f"⚪ <b>P (Central):</b> <code>{p.get('P', 0.0):.2f}</code>\n\n"

        s += f"🟢 S1: <code>{p.get('L1', 0.0):.2f}</code>\n"
        s += f"🟢 S2: <code>{p.get('L2', 0.0):.2f}</code>\n"
        s += f"🟢 S3 (Rango): <code>{p.get('L3', 0.0):.2f}</code>\n"
        s += f"🔵 S4 (Breakout): <code>{p.get('L4', 0.0):.2f}</code>\n"
        s += f"🔵 <b>S5 (Target):</b> <code>{p.get('L5', 0.0):.2f}</code>\n"
        s += f"🔵 <b>S6 (Target):</b> <code>{p.get('L6', 0.0):.2f}</code>\n"
        
        cw = p.get("width", 0)
        is_ranging = p.get("is_ranging_day", True)
        day_type = "Rango (CPR Ancho)" if is_ranging else "Tendencia (CPR Estrecho)"
        s += f"\n📅 <b>Análisis: {day_type}</b> ({cw:.2f}%)"
        
        return s
