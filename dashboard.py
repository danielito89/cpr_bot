from flask import Flask, render_template_string
import subprocess
import ccxt
import time

app = Flask(__name__)

# CONFIGURACIÓN
# Comandos para leer los logs reales de los servicios
CMD_LOG_V76 = "journalctl -u cpr_crash.service -n 15 --no-pager"
CMD_LOG_V66 = "journalctl -u cpr_bot.service -n 15 --no-pager"

# HTML TEMPLATE (Dark Institutional Mode)
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>CPR Command Center</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="60">
    <style>
        body { background-color: #0d1117; color: #c9d1d9; font-family: 'Consolas', 'Monaco', monospace; padding: 20px; }
        .card { background: #161b22; border: 1px solid #30363d; padding: 20px; margin-bottom: 20px; border-radius: 6px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { margin-top: 0; border-bottom: 1px solid #30363d; padding-bottom: 10px; font-size: 1.2rem; display: flex; align-items: center; }
        .status-dot { height: 10px; width: 10px; background-color: #bbb; border-radius: 50%; display: inline-block; margin-right: 10px; }
        .green-dot { background-color: #2ea043; box-shadow: 0 0 5px #2ea043; }
        .red-dot { background-color: #da3633; box-shadow: 0 0 5px #da3633; }
        .blue-dot { background-color: #1f6feb; box-shadow: 0 0 5px #1f6feb; }
        
        pre { background: #0d1117; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 11px; color: #8b949e; border: 1px solid #30363d; }
        
        .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 10px; text-align: center; }
        .stat-box { background: #21262d; padding: 10px; border-radius: 4px; }
        .stat-value { font-size: 1.5rem; font-weight: bold; margin-top: 5px; }
        .label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
        
        .val-green { color: #3fb950; }
        .val-yellow { color: #d29922; }
        .footer { text-align: center; font-size: 0.8rem; color: #484f58; margin-top: 30px; }
    </style>
</head>
<body>
    <h1>🏥 CPR COMMAND CENTER <span style="font-size:0.5em; color:#555;">V5.0</span></h1>

    <div class="card">
        <h2><span class="status-dot blue-dot"></span> RENTA FIJA (CARRY)</h2>
        <div class="grid-container">
            <div class="stat-box">
                <div class="label">BTC APR</div>
                <div class="stat-value val-green">{{ btc_apr }}%</div>
            </div>
            <div class="stat-box">
                <div class="label">ETH APR</div>
                <div class="stat-value val-green">{{ eth_apr }}%</div>
            </div>
            <div class="stat-box">
                <div class="label">PEPE APR</div>
                <div class="stat-value val-yellow">{{ pepe_apr }}%</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h2><span class="status-dot red-dot"></span> V76 THE SURGEON</h2>
        <pre>{{ log_v76 }}</pre>
    </div>

    <div class="card">
        <h2><span class="status-dot green-dot"></span> V66 GOLDEN CROSS</h2>
        <pre>{{ log_v66 }}</pre>
    </div>
    
    <div class="footer">
        System Status: ONLINE | Last Update: {{ last_update }}
    </div>
</body>
</html>
"""

def get_funding_rates():
    try:
        # Usamos fetch_funding_rates (plural) para mayor eficiencia y robustez
        exchange = ccxt.binance({'options': {'defaultType': 'future'}})
        symbols = ['BTC/USDT', 'ETH/USDT', '1000PEPE/USDT']
        rates = exchange.fetch_funding_rates(symbols)
        
        def safe_apr(sym):
            if sym in rates:
                rate = float(rates[sym]['fundingRate'])
                apr = rate * 3 * 365 * 100
                return f"{apr:.2f}"
            return "---"

        return safe_apr('BTC/USDT'), safe_apr('ETH/USDT'), safe_apr('1000PEPE/USDT')
    except Exception as e:
        return "Err", "Err", "Err"

def get_logs(cmd):
    try:
        return subprocess.check_output(cmd, shell=True).decode('utf-8').strip()
    except:
        return "⚠️ Error accediendo a logs o servicio detenido."

@app.route('/')
def index():
    btc, eth, pepe = get_funding_rates()
    v76_log = get_logs(CMD_LOG_V76)
    v66_log = get_logs(CMD_LOG_V66)
    now = time.strftime("%H:%M:%S UTC")
    
    return render_template_string(HTML, 
                                  btc_apr=btc, eth_apr=eth, pepe_apr=pepe,
                                  log_v76=v76_log, log_v66=v66_log, last_update=now)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)