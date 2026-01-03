from flask import Flask, render_template
import sys
import os
from datetime import datetime

# Importar tus módulos compartidos
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from shared.ccxt_handler import BinanceHandler
import config

app = Flask(__name__)
exchange = BinanceHandler()

@app.route('/')
def dashboard():
    try:
        # Obtener datos frescos
        balance = exchange.get_balance()
        positions = exchange.get_open_positions()
        
        # Calcular PnL total flotante
        total_pnl = sum([float(p['pnl']) for p in positions])
        
        # Max slots
        max_slots = config.RISK_CONFIG['MAX_OPEN_POSITIONS']
        
        return render_template(
            'index.html',
            balance=balance,
            positions=positions,
            total_pnl=total_pnl,
            max_slots=max_slots,
            last_update=datetime.now().strftime("%H:%M:%S")
        )
    except Exception as e:
        return f"<h3>Error conectando a Binance: {e}</h3>"

if __name__ == '__main__':
    # host='0.0.0.0' permite que entres desde cualquier PC/Móvil en la red
    app.run(host='0.0.0.0', port=5000, debug=False)