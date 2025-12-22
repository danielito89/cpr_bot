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
        RECONCILIACIÓN: Compara la verdad del Bot vs Exchange.
        """
        try:
            real_pos = self.api.get_position()
            bot_state = self.state.load_state()
            bot_in_pos = bot_state.get("in_position", False)
            
            # CASO A: Sincronizados
            if (real_pos is None and not bot_in_pos):
                self._heal_error() # Todo OK, sanar contador
                return True
            
            if (real_pos is not None and bot_in_pos):
                # FIX #1: Usamos el 'side' guardado, no inferido
                bot_side = bot_state.get('side', 'UNKNOWN')
                
                if real_pos['side'] == bot_side:
                    self._heal_error() # Todo OK
                    return True 
                else:
                    self.tg.send_msg(f"🚨 *CRITICAL ERROR*: Lado desalineado.\nBot: {bot_side}\nBinance: {real_pos['side']}")
                    self.emergency_flatten(real_pos, "Alignment Error")
                    return False

            # CASO B: ZOMBIE (Binance tiene posición, Bot no)
            if real_pos is not None and not bot_in_pos:
                msg = (
                    f"🧟 *ZOMBIE POSITION DETECTADA*\n"
                    f"Binance tiene {real_pos['amount']} {real_pos['side']}\n"
                    f"⚠️ *ACCIÓN*: Cerrando posición a mercado."
                )
                self.tg.send_msg(msg)
                print("🧟 ZOMBIE DETECTADO. EJECUTANDO CIERRE DE EMERGENCIA.")
                self.emergency_flatten(real_pos, "Zombie Cleanup")
                return False

            # CASO C: GHOST (Bot tiene posición, Binance no)
            if real_pos is None and bot_in_pos:
                msg = f"👻 *GHOST DETECTADO*: Limpiando estado local."
                self.tg.send_msg(msg)
                print("👻 GHOST DETECTADO. LIMPIANDO ESTADO.")
                self.state.clear_state()
                return False

            return True

        except Exception as e:
            print(f"⚠️ Error en Auditoría: {e}")
            self.errors_count += 1
            return True 

    def _heal_error(self):
        # FIX #5: Reset gradual de errores si todo está bien
        if self.errors_count > 0:
            self.errors_count -= 1

    def check_kill_switch(self, daily_pnl, consecutive_losses):
        if consecutive_losses >= self.consecutive_losses_limit:
            self.tg.send_msg(f"💀 *KILL SWITCH (Racha)*\n{consecutive_losses} pérdidas seguidas.")
            return True
            
        if daily_pnl <= -self.max_daily_loss_r:
            self.tg.send_msg(f"💀 *KILL SWITCH (Drawdown)*\nPnL Diario: {daily_pnl:.2f}R")
            return True
            
        if self.errors_count >= self.max_errors:
            self.tg.send_msg(f"💀 *KILL SWITCH (API)*\n{self.errors_count} errores de conexión.")
            return True

        return False

    def emergency_flatten(self, position, reason):
        self.api.close_position(position)
        self.state.clear_state()