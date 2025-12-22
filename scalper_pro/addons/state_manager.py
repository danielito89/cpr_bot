import json
import os

STATE_FILE = "bot_state.json"

class StateManager:
    def __init__(self):
        self.file = STATE_FILE
        if not os.path.exists(self.file):
            self.save_state({})

    def save_state(self, data):
        with open(self.file, 'w') as f:
            json.dump(data, f)

    def load_state(self):
        with open(self.file, 'r') as f:
            return json.load(f)

    # FIX #1: Agregamos argumento 'side'
    def set_entry(self, price, time_str, sl, side): 
        data = {
            "in_position": True,
            "side": side,  # <-- ALMACENADO EXPLÍCITAMENTE
            "entry_price": price,
            "entry_time": str(time_str),
            "stop_loss": sl,
            "tp1_hit": False,
            "bars_held": 0
        }
        self.save_state(data)

    def clear_state(self):
        self.save_state({"in_position": False})

    def update_bars_held(self):
        data = self.load_state()
        if data.get("in_position"):
            data["bars_held"] += 1
            self.save_state(data)
            return data["bars_held"]
        return 0
    
    def set_tp1_hit(self):
        data = self.load_state()
        if data.get("in_position"):
            data["tp1_hit"] = True
            self.save_state(data)