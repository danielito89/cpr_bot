# addons/state_manager.py
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
            json.dump(data, f, indent=4)

    def load_state(self):
        with open(self.file, 'r') as f:
            try:
                return json.load(f)
            except:
                return {}

    def get_pair_state(self, symbol):
        data = self.load_state()
        return data.get(symbol, {"in_position": False})

    def set_entry(self, symbol, price, time_str, sl, side): 
        data = self.load_state()
        data[symbol] = {
            "in_position": True,
            "side": side,
            "entry_price": price,
            "entry_time": str(time_str),
            "stop_loss": sl,
            "tp1_hit": False,
            "bars_held": 0
        }
        self.save_state(data)

    def clear_pair_state(self, symbol):
        data = self.load_state()
        if symbol in data:
            data[symbol] = {"in_position": False}
            self.save_state(data)

    def update_bars_held(self, symbol):
        data = self.load_state()
        if symbol in data and data[symbol].get("in_position"):
            data[symbol]["bars_held"] += 1
            self.save_state(data)
            return data[symbol]["bars_held"]
        return 0
    
    def set_tp1_hit(self, symbol):
        data = self.load_state()
        if symbol in data and data[symbol].get("in_position"):
            data[symbol]["tp1_hit"] = True
            self.save_state(data)