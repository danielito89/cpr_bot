from flask import Flask, render_template_string
import subprocess
import os
import ccxt

app = Flask(__name__)

# CONFIG
LOG_FILE_V66 = "logs_v66_ficticio.txt" # Ojo: Necesitaremos que tus bots escriban un estado legible
LOG_SYSTEMD_V76 = "journalctl -u cpr_crash.service -n 10 --no-pager"

# HTML TEMPLATE (Sencillo y Oscuro)
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>CPR Command Center</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { background-color: #121212; color: #e0e0e0; font-family: monospace; padding: 20px; }
        .card { background: #1e1e1e; border: 1px solid #333; padding: 15px; margin-bottom: 20px; border-radius: 8px; }
        h2 { margin-top: 0; border-bottom: 1px solid #333; padding-bottom: 10px; }
        .green { color: #00e676; }
        .red { color: #ff5252; }
        .blue { color: #2979ff; }
        .yellow { color: #ffea00; }
        pre { white-space: pre-wrap; font-size: 12px; }
        .stat { font-size: 24px; font-weight: bold; }
        .label { font-size: 12px; color: #888; }
    </style>
    <meta http-equiv="refresh" content="60"> </head>
<body>
    <h1>🏥 CPR COMMAND CENTER</h1>

    <div class="card">
        <h2 class="blue">💰 Renta Fija (Cash & Carry)</h2>
        <div style="display: flex; justify-content: space-around;">
            <div><div class="label">BTC APR</div><div class="stat">{{ btc_apr }}%</div></div>
            <div><div class="label">ETH APR</div><div class="stat">{{ eth_apr }}%</div></div>
            <div><div class="label">PEPE APR</div><div class="stat yellow">{{ pepe_apr }}%</div></div>
        </div>
    </div>

    <div class="card">
        <h2 class="red">🐻 V76 The Surgeon (Crash Bot)</h2>
        <pre>{{ log_v76 }}</pre>
    </div>

    <div class="card">
        <h2 class="green">🐂 V66 Golden Cross</h2>
        <p>Estado del servicio:</p>
        <pre>{{ log_v66 }}</pre>
    </div>
    
    <div style="text-align: center; color: #555; font-size: 10px;">
        Actualizado automáticamente cada minuto.
    </div>
</body>
</html>
"""

def get_funding_rates():
    try:
        # Usamos fetch_funding_rates (plural) que es más directo para esto
        exchange = ccxt.binance({'options': {'defaultType': 'future'}})
        # Mapeo de símbolos común en ccxt
        symbols = ['BTC/USDT', 'ETH/USDT', '1000PEPE/USDT']
        
        rates = exchange.fetch_funding_rates(symbols)
        
        def safe_apr(sym):
            if sym in rates:
                rate = rates[sym]['fundingRate']
                return f"{float(rate) * 3 * 365 * 100:.2f}"
            return "Err"

        return safe_apr('BTC/USDT'), safe_apr('ETH/USDT'), safe_apr('1000PEPE/USDT')
    except Exception as e:
        print(f"Error fetching funding: {e}") # Para ver en consola si falla
        return "Err", "Err", "Err"

def get_v76_logs():
    try:
        # Leer las ultimas 10 lineas del servicio real
        result = subprocess.check_output(LOG_SYSTEMD_V76, shell=True).decode('utf-8')
        return result
    except:
        return "Error leyendo logs de systemd."

def get_v66_logs():
    # Como V66 aun no es servicio, leemos procesos o un log ficticio por ahora
    # TODO: Convertir V66 a servicio systemd igual que V76
    try:
        return subprocess.check_output("ps aux | grep main_bot.py | grep -v grep", shell=True).decode('utf-8')
    except:
        return "⚠️ Bot V66 no parece estar corriendo."

@app.route('/')
def index():
    btc_apr, eth_apr, pepe_apr = get_funding_rates()
    log_v76 = get_v76_logs()
    log_v66 = get_v66_logs()
    
    return render_template_string(HTML, 
                                  btc_apr=btc_apr, 
                                  eth_apr=eth_apr, 
                                  pepe_apr=pepe_apr,
                                  log_v76=log_v76,
                                  log_v66=log_v66)

if __name__ == '__main__':
    # Correr en puerto 5000 accesible desde fuera (0.0.0.0)
    app.run(host='0.0.0.0', port=5000)