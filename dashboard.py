from flask import Flask, render_template_string
import subprocess
import ccxt
import time

app = Flask(__name__)

# ---------------- CONFIGURACIÓN ----------------
# Comandos para leer los logs reales
CMD_LOG_V76 = "journalctl -u cpr_crash.service -n 15 --no-pager"
CMD_LOG_V66 = "journalctl -u cpr_bot.service -n 15 --no-pager"
# Log del nuevo bot de Renta Fija
CMD_LOG_CARRY = "tail -n 15 /home/ubuntu/bot_cpr/logs/carry.log"

# ---------------- HTML TEMPLATE ----------------
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
        .purple-dot { background-color: #8957e5; box-shadow: 0 0 5px #8957e5; }
        
        pre { background: #0d1117; padding: 10px; border-radius: 4px; overflow-x: auto; font-size: 11px; color: #8b949e; border: 1px solid #30363d; white-space: pre-wrap; word-wrap: break-word; }
        
        .grid-container { display: grid; grid-template-columns: repeat(auto-fit, minmax(100px, 1fr)); gap: 10px; text-align: center; }
        .stat-box { background: #21262d; padding: 10px; border-radius: 4px; }
        .stat-value { font-size: 1.5rem; font-weight: bold; margin-top: 5px; }
        .label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; }
        
        .val-green { color: #3fb950; }
        .footer { text-align: center; font-size: 0.8rem; color: #484f58; margin-top: 30px; }
    </style>
</head>
<body>
    <h1>🏥 CPR COMMAND CENTER <span style="font-size:0.5em; color:#555;">V5.3 (CORE)</span></h1>

    <div class="card">
        <h2><span class="status-dot blue-dot"></span> RENTA FIJA (APR ESTIMADO)</h2>
        <div class="grid-container">
            <div class="stat-box">
                <div class="label">BTC APR</div>
                <div class="stat-value val-green">{{ btc_apr }}</div>
            </div>
            <div class="stat-box">
                <div class="label">ETH APR</div>
                <div class="stat-value val-green">{{ eth_apr }}</div>
            </div>
        </div>
    </div>

    <div class="card">
        <h2><span class="status-dot purple-dot"></span> CARRY BOT (RENTA FIJA)</h2>
        <pre>{{ log_carry }}</pre>
    </div>

    <div class="card">
        <h2><span class="status-dot red-dot"></span> CRASH BOT</h2>
        <pre>{{ log_v76 }}</pre>
    </div>

    <div class="card">
        <h2><span class="status-dot green-dot"></span> GOLDEN CROSS BOT</h2>
        <pre>{{ log_v66 }}</pre>
    </div>
    
    <div class="footer">
        System Status: ONLINE | Last Update: {{ last_update }}
    </div>
</body>
</html>
"""

# ---------------- LÓGICA BACKEND ----------------

def get_funding_rates():
    try:
        exchange = ccxt.binance({'enableRateLimit': True, 'options': {'defaultType': 'future'}})
        targets = ['BTC/USDT', 'ETH/USDT']
        results = {}

        for symbol in targets:
            try:
                funding_info = exchange.fetch_funding_rate(symbol)
                rate = float(funding_info['fundingRate'])
                # Calculo APR anualizado simple
                apr_val = rate * 3 * 365 * 100
                results[symbol] = f"{apr_val:.2f}%"
            except:
                results[symbol] = "---"
        
        return results.get('BTC/USDT'), results.get('ETH/USDT')

    except Exception as e:
        return "Err", "Err"

def get_logs(cmd):
    try:
        # Timeout de 3s para lectura rápida
        output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=3)
        return output.decode('utf-8').strip()
    except subprocess.TimeoutExpired:
        return "⚠️ Timeout leyendo logs (Sistema ocupado)"
    except subprocess.CalledProcessError:
        return f"⚠️ No hay logs recientes o servicio detenido."
    except Exception as e:
        return f"⚠️ Error: {str(e)}"

@app.route('/')
def index():
    btc, eth = get_funding_rates()
    v76_log = get_logs(CMD_LOG_V76)
    v66_log = get_logs(CMD_LOG_V66)
    carry_log = get_logs(CMD_LOG_CARRY)
    
    now = time.strftime("%H:%M:%S")
    
    return render_template_string(HTML, 
                                  btc_apr=btc, 
                                  eth_apr=eth, 
                                  log_v76=v76_log, 
                                  log_v66=v66_log, 
                                  log_carry=carry_log,
                                  last_update=now)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)