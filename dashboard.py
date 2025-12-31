from flask import Flask, render_template_string
import subprocess
import json
import os
import glob
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
# Ajusta esta ruta a donde tengas la carpeta raíz de tu suite
BASE_PATH = "/home/ubuntu/bot_cpr" 

# Rutas de estados
SCALPER_STATE = os.path.join(BASE_PATH, "bots", "scalper", "state_scalper.json")
BREAKOUT_DIR = os.path.join(BASE_PATH, "bots", "breakout")

# Servicios de Systemd (para los logs)
SERVICES = ["scalper_pro", "breakout_sol"] 

# --- HTML TEMPLATE (DARK MODE PRO - UPDATED) ---
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>HYDRA COMMAND CENTER</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="10">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9; --accent: #58a6ff; --green: #2ea043; --red: #da3633; --orange: #d29922; --purple: #a371f7; }
        body { background-color: var(--bg); color: var(--text); font-family: 'JetBrains Mono', monospace; padding: 20px; margin: 0; }
        .container { max-width: 1200px; margin: 0 auto; display: grid; gap: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; }
        
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
        .stat-card { background: var(--card); border: 1px solid var(--border); padding: 15px; border-radius: 6px; text-align: center; }
        .stat-value { font-size: 1.5rem; font-weight: bold; color: var(--accent); }
        .stat-label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; }
        .warning { color: var(--orange); } .danger { color: var(--red); }

        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
        th { color: #8b949e; font-size: 0.8rem; text-transform: uppercase; }
        .badge { padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
        .badge-long { background: rgba(46, 160, 67, 0.15); color: var(--green); border: 1px solid var(--green); }
        .badge-short { background: rgba(218, 54, 51, 0.15); color: var(--red); border: 1px solid var(--red); }
        .badge-bot { background: rgba(88, 166, 255, 0.15); color: var(--accent); border: 1px solid var(--border); margin-right: 5px;}
        .badge-breakout { background: rgba(163, 113, 247, 0.15); color: var(--purple); border: 1px solid var(--border); margin-right: 5px;}

        .log-box { background: #000; padding: 15px; border-radius: 6px; border: 1px solid var(--border); height: 400px; overflow-y: auto; font-size: 0.85rem; color: #8b949e; white-space: pre-wrap; }
        h2 { font-size: 1rem; margin-top: 0; color: var(--text); display: flex; align-items: center; gap: 10px; }
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .dot-green { background: var(--green); box-shadow: 0 0 8px var(--green); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 style="margin:0; font-size:1.5rem;">🐲 HYDRA <span style="color:var(--accent)">SUITE</span></h1>
                <div style="font-size:0.8rem; color:#8b949e; margin-top:5px;">Scalper & Breakout Integration | Multi-Service</div>
            </div>
            <div style="text-align:right;">
                <div style="font-size:1.2rem;">{{ time_now }}</div>
                <div style="font-size:0.8rem; color:#8b949e;">UTC TIME</div>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value {{ cpu_color }}">{{ cpu_usage }}</div>
                <div class="stat-label">CPU LOAD</div>
            </div>
            <div class="stat-card">
                <div class="stat-value {{ ram_color }}">{{ ram_usage }}</div>
                <div class="stat-label">RAM USAGE</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color:var(--text)">{{ active_positions_count }}</div>
                <div class="stat-label">TOTAL POSITIONS</div>
            </div>
        </div>

        <div style="background:var(--card); border:1px solid var(--border); padding:20px; border-radius:6px;">
            <h2><span class="dot dot-green"></span> ACTIVE POSITIONS</h2>
            {% if positions %}
            <table>
                <thead>
                    <tr>
                        <th>BOT</th>
                        <th>PAIR</th>
                        <th>SIDE</th>
                        <th>ENTRY</th>
                        <th>SL / TP</th>
                        <th>STATUS</th>
                    </tr>
                </thead>
                <tbody>
                    {% for p in positions %}
                    <tr>
                        <td>
                            <span class="badge {{ 'badge-bot' if p.bot == 'SCALPER' else 'badge-breakout' }}">
                                {{ p.bot }}
                            </span>
                        </td>
                        <td style="font-weight:bold; color:#fff;">{{ p.symbol }}</td>
                        <td><span class="badge {{ 'badge-long' if p.side == 'LONG' else 'badge-short' }}">{{ p.side }}</span></td>
                        <td>${{ p.entry }}</td>
                        <td>SL: ${{ p.sl }} <br> <span style="color:#8b949e; font-size:0.8em">TP: {{ p.tp }}</span></td>
                        <td>{{ p.status }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="padding:20px; text-align:center; color:#8b949e; font-style:italic;">
                Scanning markets... No active positions in ecosystem.
            </div>
            {% endif %}
        </div>

        <div>
            <h2>SYSTEM LOGS (Merged)</h2>
            <div class="log-box">{{ logs }}</div>
        </div>

    </div>
</body>
</html>
"""

# --- BACKEND LOGIC ---

def get_sys_stats():
    """Métricas del servidor"""
    try:
        load = os.getloadavg()[0]
        out = subprocess.check_output("free -m", shell=True).decode()
        lines = out.split('\n')
        mem_line = [x for x in lines[1].split() if x]
        total_mem = int(mem_line[1])
        used_mem = int(mem_line[2])
        ram_pct = int((used_mem / total_mem) * 100)
        
        return {
            'cpu': f"{load:.2f}",
            'cpu_color': 'danger' if load > 2.0 else 'warning' if load > 1.0 else '',
            'ram': f"{ram_pct}%",
            'ram_color': 'danger' if ram_pct > 90 else 'warning' if ram_pct > 75 else ''
        }
    except:
        return {'cpu': 'ERR', 'ram': 'ERR', 'cpu_color':'', 'ram_color':''}

def get_positions_combined():
    """Lee Scalper + Todos los Breakout States"""
    positions = []
    
    # 1. LEER SCALPER (Archivo único con múltiples pares)
    if os.path.exists(SCALPER_STATE):
        try:
            with open(SCALPER_STATE, 'r') as f:
                data = json.load(f)
                # El formato del Scalper suele ser { "BTC/USDT": { ... }, "ETH/USDT": { ... } }
                for symbol, info in data.items():
                    # Verificar si está realmente en posición
                    if info.get('in_position') or info.get('entry_price'):
                         positions.append({
                            'bot': 'SCALPER',
                            'symbol': symbol,
                            'side': info.get('side', 'LONG'), # Asumimos Long si no dice nada
                            'entry': info.get('entry_price', 0),
                            'sl': f"{info.get('stop_loss', 0):.4f}",
                            'tp': 'Dynamic',
                            'status': 'Active'
                        })
        except Exception as e:
            print(f"Error reading Scalper state: {e}")

    # 2. LEER BREAKOUT (Múltiples archivos state_*.json)
    # Buscamos todos los archivos que empiecen con state_ en la carpeta breakout
    breakout_files = glob.glob(os.path.join(BREAKOUT_DIR, "state_*.json"))
    
    for file_path in breakout_files:
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
                # El formato del Breakout es { "status": "IN_POSITION", ... }
                if data.get('status') == 'IN_POSITION':
                    # Deducimos el símbolo del nombre del archivo o si está en el json (mejor si estuviera en json, si no parseamos nombre)
                    filename = os.path.basename(file_path)
                    symbol_raw = filename.replace("state_", "").replace(".json", "").replace("_", "/")
                    
                    positions.append({
                        'bot': 'BREAKOUT',
                        'symbol': symbol_raw,
                        'side': 'LONG', # Breakout strategy es Long Only por ahora
                        'entry': data.get('entry_price'),
                        'sl': f"{data.get('stop_loss'):.4f}",
                        'tp': f"{data.get('tp_partial'):.4f}",
                        'status': 'Trailing' if data.get('trailing_active') else 'Targeting'
                    })
        except Exception as e:
            print(f"Error reading Breakout file {file_path}: {e}")

    return positions

def get_logs():
    try:
        # Traemos logs de AMBOS servicios
        service_flags = " ".join([f"-u {s}" for s in SERVICES])
        cmd = f"journalctl {service_flags} -n 50 --no-pager"
        return subprocess.check_output(cmd, shell=True).decode().strip()
    except: return "Error reading logs."

@app.route('/')
def index():
    stats = get_sys_stats()
    positions = get_positions_combined()
    logs = get_logs()
    
    return render_template_string(HTML, 
                                  cpu_usage=stats['cpu'], cpu_color=stats['cpu_color'],
                                  ram_usage=stats['ram'], ram_color=stats['ram_color'],
                                  active_positions_count=len(positions),
                                  positions=positions,
                                  logs=logs,
                                  time_now=datetime.utcnow().strftime('%H:%M:%S'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)