import os
import json
import logging
from datetime import datetime
from bot_core.utils import atomic_save_json # Importamos nuestra nueva función

class StateManager:
    def __init__(self, state_file_path):
        self.STATE_FILE = state_file_path

        # --- Estado de Indicadores ---
        self.cached_atr = None
        self.cached_ema = None
        self.cached_median_vol = None 
        self.daily_pivots = {}
        self.last_pivots_date = None

        # --- Estado de Posición ---
        self.is_in_position = False
        self.current_position_info = {}
        self.last_known_position_qty = 0.0
        self.sl_moved_to_be = False
        self.trade_cooldown_until = 0

        # --- Estado de PnL ---
        self.daily_trade_stats = []
        self.start_of_day = datetime.utcnow().date()
        self.daily_start_balance = None

        # --- Estado de Control ---
        self.trading_paused = False

    def get_state_snapshot(self):
        """Crea un diccionario de todas las variables de estado para guardar."""
        # Convertir fechas a string para JSON
        state = self.__dict__.copy()
        state['last_pivots_date'] = str(self.last_pivots_date) if self.last_pivots_date else None
        state['start_of_day'] = str(self.start_of_day) if self.start_of_day else None
        return state

    def save_state(self):
        """Guarda el snapshot actual del estado en el archivo."""
        state_data = self.get_state_snapshot()
        atomic_save_json(state_data, self.STATE_FILE)

    def load_state(self):
        """Carga el estado desde el archivo y actualiza las variables."""
        if not os.path.exists(self.STATE_FILE):
            logging.info("No state file, iniciando limpio.")
            return
        try:
            with open(self.STATE_FILE, "r") as f:
                state = json.load(f)

            # --- NUEVO: Validar si el estado es de hoy ---
            saved_date_str = state.get("start_of_day")
            today_str = str(datetime.utcnow().date())
            
            # Si el archivo es de otro día, lo ignoramos para limpiar límites viejos
            if saved_date_str and saved_date_str != today_str:
                logging.warning(f"Estado obsoleto detectado ({saved_date_str}). Iniciando día limpio.")
                # No cargamos nada, el __init__ ya puso los valores por defecto vacíos
                return 
            # ---------------------------------------------

            # Cargar estado (con valores por defecto si faltan)
            self.is_in_position = state.get("is_in_position", False)
            self.current_position_info = state.get("current_position_info", {})
            self.sl_moved_to_be = state.get("sl_moved_to_be", False)
            self.last_known_position_qty = state.get("last_known_position_qty", 0.0)
            self.trade_cooldown_until = state.get("trade_cooldown_until", 0)
            self.daily_trade_stats = state.get("daily_trade_stats", [])

            lp = state.get("last_pivots_date")
            self.last_pivots_date = datetime.fromisoformat(lp).date() if lp else None

            sd = state.get("start_of_day")
            self.start_of_day = datetime.fromisoformat(sd).date() if sd else datetime.utcnow().date()

            self.cached_atr = state.get("cached_atr")
            self.cached_ema = state.get("cached_ema")
            self.cached_median_vol = state.get("cached_median_vol")
            self.cached_adx = state.get("cached_adx")
            self.trading_paused = state.get("trading_paused", False)
            self.daily_start_balance = state.get("daily_start_balance", None)

            logging.info("Estado cargado exitosamente.")
        except Exception as e:
            logging.error(f"Error cargando estado, iniciando limpio: {e}")