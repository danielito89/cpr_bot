import json
import os

class StateManager:
    def __init__(self, filename="bot_state.json"):
        # Guardamos en la carpeta raíz del proyecto para que sea persistente
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.filepath = os.path.join(base_dir, filename)
        self.state = self._load_state()

    def _load_state(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_state(self):
        with open(self.filepath, 'w') as f:
            json.dump(self.state, f, indent=4)

    # --- MÉTODOS QUE FALTABAN ---
    
    def get_position(self, symbol):
        """Devuelve el estado si existe, o None si está libre"""
        return self.state.get(symbol)

    def set_entry(self, symbol, price, time, sl, side):
        """Registra una nueva entrada"""
        self.state[symbol] = {
            'entry_price': price,
            'entry_time': str(time),
            'sl': sl,
            'side': side,
            'status': 'OPEN'
        }
        self._save_state()

    def clear_position(self, symbol):
        """Borra la posición del registro"""
        if symbol in self.state:
            del self.state[symbol]
            self._save_state()
            
    def get_all_active_symbols(self):
        """Devuelve lista de símbolos activos"""
        return list(self.state.keys())