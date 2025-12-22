# core/production_controller.py
import time
from datetime import datetime

class ProductionController:
    def __init__(self, api, state_manager, telegram_bot, config):
        self.api = api
        self.state = state_manager
        self.tg = telegram_bot
        self.config = config
        self.errors_count = 0
        self.max_errors = 5
        
        # Kill Switch Limits
        self.max_daily_loss_r = 3.0
        self.consecutive_losses_limit = 3

    def audit_positions(self):
        """
        RECONCILIACIÓN: Compara la verdad del Bot (state.json) 
        vs la verdad del Exchange (Binance API).
        """
        try:
            # 1. Obtener verdades
            real_pos = self.api.get_position() # Binance
            bot_state = self.state.load_state() # JSON
            bot_in_pos = bot_state.get("in_position", False)
            
            # CASO A: Sincronizados (Ambos vacíos o ambos con posición)
            if (real_pos is None and not bot_in_pos):
                return True # Todo OK (Flat)
            
            if (real_pos is not None and bot_in_pos):
                # Verificar que sea el mismo lado (LONG vs LONG)
                if real_pos['side'] == ('LONG' if bot_state['tp1_price'] > bot_state['entry_price'] else 'SHORT'):
                    # Verificar que el tamaño sea similar (tolerancia por redondeo)
                    # Esto es opcional, pero buena práctica. Por ahora asumimos OK.
                    return True 
                else:
                    # Lado incorrecto! Bot dice LONG, Binance dice SHORT.
                    self.tg.send_msg(f"🚨 *CRITICAL ERROR*: Lado desalineado.\nBot: {bot_in_pos}\nBinance: {real_pos['side']}")
                    self.emergency_flatten(real_pos, "Alignment Error")
                    return False

            # CASO B: ZOMBIE POSITION (Binance tiene posición, Bot no)
            # PELIGROSO: El bot se olvidó de la posición.
            if real_pos is not None and not bot_in_pos:
                msg = (
                    f"🧟 *ZOMBIE POSITION DETECTADA*\n"
                    f"Binance tiene {real_pos['amount']} {real_pos['side']}\n"
                    f"El Bot creía estar FLAT.\n"
                    f"⚠️ *ACCIÓN*: Cerrando posición a mercado."
                )
                self.tg.send_msg(msg)
                print("🧟 ZOMBIE DETECTADO. EJECUTANDO CIERRE DE EMERGENCIA.")
                self.emergency_flatten(real_pos, "Zombie Cleanup")
                return False

            # CASO C: GHOST POSITION (Bot tiene posición, Binance no)
            # MOLESTO: El bot quiere gestionar algo que ya no existe (SL saltó externamente).
            if real_pos is None and bot_in_pos:
                msg = (
                    f"👻 *GHOST POSITION DETECTADA*\n"
                    f"El Bot cree estar IN, pero Binance está FLAT.\n"
                    f"Causa probable: SL saltó o cierre manual.\n"
                    f"ℹ️ *ACCIÓN*: Limpiando estado del bot."
                )
                self.tg.send_msg(msg)
                print("👻 GHOST DETECTADO. LIMPIANDO ESTADO.")
                self.state.clear_state()
                return False

            return True

        except Exception as e:
            print(f"⚠️ Error en Auditoría: {e}")
            self.errors_count += 1
            return True # Asumimos OK para no paniquear por un timeout de API

    def check_kill_switch(self, daily_pnl, consecutive_losses):
        """
        Verifica si debemos apagar el bot por seguridad financiera.
        """
        # 1. Racha Perdedora
        if consecutive_losses >= self.consecutive_losses_limit:
            msg = f"💀 *KILL SWITCH (Racha)*\n{consecutive_losses} pérdidas seguidas.\nApagando hasta mañana."
            self.tg.send_msg(msg)
            return True
            
        # 2. Pérdida Diaria Máxima
        if daily_pnl <= -self.max_daily_loss_r:
            msg = f"💀 *KILL SWITCH (Drawdown)*\nPnL Diario: {daily_pnl:.2f}R\nLímite: -{self.max_daily_loss_r}R\nApagando hasta mañana."
            self.tg.send_msg(msg)
            return True
            
        # 3. Errores de API (Si falló 5 veces seguidas la auditoría)
        if self.errors_count >= self.max_errors:
            msg = f"💀 *KILL SWITCH (API)*\nDemasiados errores de conexión ({self.errors_count}).\nRevisar servidor."
            self.tg.send_msg(msg)
            return True

        return False

    def emergency_flatten(self, position, reason):
        """Cierra todo y limpia estado"""
        self.api.close_position(position)
        self.state.clear_state()