import asyncio
import logging
import httpx

class TelegramHandler:
    def __init__(self, orchestrator, token, chat_id):
        """
        :param orchestrator: Referencia al BotOrchestrator (main_v81).
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
            
            # Parsear comando y argumentos (ej: "/start BTCUSDT")
            parts = text.split()
            cmd = parts[0].lower()
            arg = parts[1].upper() if len(parts) > 1 else None

            # --- COMANDOS ---

            if cmd == "/status":
                # Si hay argumento (/status BTCUSDT), mostrar solo ese. Si no, todos.
                report = self._generate_multibot_status(target_symbol=arg)
                await self._send_message(report)

            elif cmd == "/pivots":
                if arg:
                    # Pivotes de un par específico
                    bot = self.orchestrator.strategies.get(arg)
                    if bot: await self._send_message(self._generate_pivots_text(bot))
                    else: await self._send_message(f"⚠️ No encuentro el bot {arg}")
                else:
                    # Pivotes de todos
                    for bot in self.orchestrator.strategies.values():
                        await self._send_message(self._generate_pivots_text(bot))

            # --- GESTIÓN DINÁMICA ---
            
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

            # --- COMANDOS DE CONTROL (Afectan al par especificado o a todos) ---

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

            elif cmd == "/limit":
                 await self._send_message(f"Límite de pérdida diaria: {self.orchestrator.DEFAULT_CONFIG['DAILY_LOSS_LIMIT_PCT']}%")
            
            elif cmd == "/restart":
                 await self._send_message("♻️ Reiniciando Orquestador...")
                 await self.orchestrator.shutdown()

            else:
                await self._send_message("❓ Comandos: /start SYM, /stop SYM, /status, /list, /pivots, /pausar, /resumir, /cerrar SYM")

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
        # (Lógica v68 adaptada para leer de 'bot.state')
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

        # Indicadores compactos
        atr = f"{bot.state.cached_atr:.2f}" if bot.state.cached_atr else "-"
        vol = f"{bot.state.cached_median_vol/1000:.1f}k" if bot.state.cached_median_vol else "-"
        s += f"📈 ATR: {atr} | VolMed: {vol}"
        
        return s

    def _generate_pivots_text(self, bot):
        p = bot.state.daily_pivots
        if not p: return f"<b>{bot.symbol}</b>: Sin pivotes."
        s = f"📊 <b>{bot.symbol} Pivotes</b>\n"
        s += f"R4: {p.get('H4')} | R3: {p.get('H3')}\n"
        s += f"--- P: {p.get('P')} ---\n"
        s += f"S3: {p.get('L3')} | S4: {p.get('L4')}"
        return s
