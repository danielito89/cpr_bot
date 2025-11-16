import asyncio
import logging

class StreamManager:
    def __init__(self, bot_controller, risk_manager, telegram_handler):
        """
        Inicializa el gestor de streams.

        :param bot_controller: La instancia principal (para self.bsm, self.running, etc.)
        :param risk_manager: La instancia de RiskManager (para llamar a check_position_state).
        :param telegram_handler: La instancia de TelegramHandler (para enviar alertas).
        """
        self.bot = bot_controller
        self.bsm = bot_controller.bsm
        self.risk_manager = risk_manager
        self.telegram_handler = telegram_handler
        self.state = bot_controller.state
        self.symbol = bot_controller.symbol
        self.account_poll_interval = bot_controller.account_poll_interval

    async def get_tasks(self):
        """Devuelve todas las tareas de streaming para ejecutar con asyncio.gather."""
        return [
            asyncio.create_task(self.run_kline_loop()),
            asyncio.create_task(self.run_user_data_loop()),
            asyncio.create_task(self.account_poller_loop()),
            asyncio.create_task(self.telegram_handler.start_polling())
        ]

    # --- KLINE (Para buscar entradas) ---
    async def run_kline_loop(self):
        logging.info("Connecting WS (Klines) 1m...")
        stream_ctx = self.bsm.kline_socket(symbol=self.symbol.lower(), interval="1m")

        while self.bot.running:
            try:
                async with stream_ctx as ksocket:
                    logging.info("WS (Klines) conectado, escuchando 1m klines...")
                    while self.bot.running:
                        msg = await ksocket.recv() 
                        if msg:
                            await self._handle_kline_evt(msg)

            except Exception as e:
                logging.error(f"WS (Klines) recv/handle error: {e}")
                await self.telegram_handler._send_message("🚨 <b>WS KLINE ERROR</b>\nReiniciando conexión.")
                await asyncio.sleep(5)

    async def _handle_kline_evt(self, msg):
        if not msg: return
        if msg.get("e") == "error":
            logging.error(f"WS error event: {msg}")
            return
        k = msg.get("k", {})
        if not k.get("x", False): return

        # Pasa la vela al RiskManager para que busque trades
        if not self.state.is_in_position:
            await self.risk_manager.seek_new_trade(k)

    # --- USER DATA (Para gestión de posición instantánea) ---
    async def run_user_data_loop(self):
        logging.info("User Data Stream (UDS) conectando...")

        while self.bot.running:
            try:
                async with self.bsm.futures_user_socket() as user_socket:
                    logging.info("User Data Stream (UDS) conectado.")
                    while self.bot.running:
                        msg = await user_socket.recv()
                        if msg:
                            await self._handle_user_data_message(msg)

            except Exception as e:
                logging.error(f"Error en User Data Stream (UDS): {e}. Reconectando en 5s...")
                await self.telegram_handler._send_message("🔌 <b>ALERTA UDS</b>\nStream de usuario desconectado. Reconectando...")
                await asyncio.sleep(5)

    async def _handle_user_data_message(self, msg):
        try:
            event_type = msg.get('e')

            if event_type == 'ORDER_TRADE_UPDATE':
                order_data = msg.get('o', {})
                if (order_data.get('s') == self.symbol and 
                    order_data.get('X') == 'FILLED'):

                    order_type = order_data.get('o')
                    if order_type in [STOP_MARKET, TAKE_PROFIT_MARKET]:
                        logging.info(f"UDS: ¡Evento de {order_type} detectado! Forzando chequeo de posición.")
                        # Llama al RiskManager
                        await self.risk_manager.check_position_state()

        except Exception as e:
            logging.error(f"Error al manejar mensaje de UDS: {e}", exc_info=True)

    # --- POLLER (Red de seguridad) ---
    async def account_poller_loop(self):
        logging.info("Poller de cuenta iniciado (intervalo %.1fs)", self.account_poll_interval)
        while self.bot.running:
            # Llama al RiskManager
            await self.risk_manager.check_position_state()
            await asyncio.sleep(self.account_poll_interval)
