from flask import Flask, render_template_string
import subprocess
import json
import os
import glob
import sys
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
# Ajusta esta ruta a la carpeta raíz de tu bot
BASE_PATH = "/home/ubuntu/bot_cpr" 

# Importamos la configuración para saber qué par es qué
sys.path.append(BASE_PATH)
try:
    import config
    PAIRS_FAST = getattr(config, 'PAIRS_FAST', [])
    PAIRS_SLOW = getattr(config, 'PAIRS_SLOW', [])
except ImportError:
    PAIRS_FAST = []
    PAIRS_SLOW = []

# Rutas de estados
BREAKOUT_DIR = os.path.join(BASE_PATH, "bots", "breakout")

# Servicios de Systemd para leer logs
SERVICES = ["breakout_fast", "breakout_slow"] 

# --- HTML TEMPLATE (DARK MODE ULTRA) ---
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>HYDRA HIBRID CENTER</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="10">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #090c10; --card: #161b22; --border: #30363d; --text: #c9d1d9; --accent: #58a6ff; --green: #2ea043; --red: #da3633; --orange: #d29922; --purple: #a371f7; --fire: #f0883e; }
        body { background-color: var(--bg); color: var(--text); font-family: 'JetBrains Mono', monospace; padding: 20px; margin: 0; }
        .container { max-width: 1200px; margin: 0 auto; display: grid; gap: 20px; }
        
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; }
        
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
        .stat-card { background: var(--card); border: 1px solid var(--border); padding: 15px; border-radius: 6px; text-align: center; }
        .stat-value { font-size: 1.5rem; font-weight: bold; color: var(--accent); }
        .stat-label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; }
        
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
        th { color: #8b949e; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1px; }
        
        .badge { padding: 3px 8px; border-radius: 12px; font-size: 0.75rem; font-weight: bold; border: 1px solid transparent; }
        .badge-long { background: rgba(46, 160, 67, 0.15); color: var(--green); border-color: var(--green); }
        .badge-short { background: rgba(218, 54, 51, 0.15); color: var(--red); border-color: var(--red); }
        
        .badge-fast { background: rgba(240, 136, 62, 0.15); color: var(--fire); border-color: var(--fire); }
        .badge-slow { background: rgba(88, 166, 255, 0.15); color: var(--accent); border-color: var(--accent); }

        .log-box { background: #000; padding: 15px; border-radius: 6px; border: 1px solid var(--border); height: 450px; overflow-y: auto; font-size: 0.80rem; color: #8b949e; white-space: pre-wrap; font-family: 'Courier New', monospace; }
        
        h2 { font-size: 1rem; margin-top: 0; color: var(--text); display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin-bottom: 15px;}
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .dot-green { background: var(--green); box-shadow: 0 0 8px var(--green); }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 style="margin:0; font-size:1.5rem;">🐲 HYDRA <span style="color:var(--fire)">HYBRID</span></h1>
                <div style="font-size:0.8rem; color:#8b949e; margin-top:5px;">Engine: Breakout | Dual Velocity (1H/4H)</div>
            </div>
            <div style="text-align:right;">
                <div style="font-size:1.2rem; font-weight:bold;">{{ time_now }}</div>
                <div style="font-size:0.8rem; color:#8b949e;">UTC SYSTEM</div>
            </div>
        </div>

        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value" style="color:{{ cpu_color }}">{{ cpu_usage }}</div>
                <div class="stat-label">CPU</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color:{{ ram_color }}">{{ ram_usage }}</div>
                <div class="stat-label">RAM</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color:var(--text)">{{ active_positions_count }}</div>
                <div class="stat-label">OPEN TRADES</div>
            </div>
        </div>

        <div style="background:var(--card); border:1px solid var(--border); padding:20px; border-radius:6px;">
            <h2><span class="dot dot-green"></span> ACTIVE POSITIONS</h2>
            {% if positions %}
            <table>
                <thead>
                    <tr>
                        <th>ENGINE</th>
                        <th>PAIR</th>
                        <th>SIDE</th>
                        <th>ENTRY</th>
                        <th>STOP LOSS</th>
                        <th>PROFIT TARGET</th>
                        <th>STATUS</th>
                    </tr>
                </thead>
                <tbody>
                    {% for p in positions %}
                    <tr>
                        <td>
                            <span class="badge {{ 'badge-fast' if p.type == 'FAST' else 'badge-slow' }}">
                                {{ '🔥 FAST 1H' if p.type == 'FAST' else '🐢 SLOW 4H' }}
                            </span>
                        </td>
                        <td style="font-weight:bold; color:#fff;">{{ p.symbol }}</td>
                        <td><span class="badge {{ 'badge-long' if p.side == 'LONG' else 'badge-short' }}">{{ p.side }}</span></td>
                        <td>${{ p.entry }}</td>
                        <td style="color:var(--red)">${{ p.sl }}</td>
                        <td style="color:var(--green)">${{ p.tp }}</td>
                        <td>
                            {% if p.trailing %}
                                <span style="color:var(--accent)">🛡️ Trailing</span>
                            {% else %}
                                <span style="color:#8b949e">Targeting</span>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="padding:40px; text-align:center; color:#8b949e; font-style:italic;">
                <div>⚡ All systems operational. Scanning for breakouts...</div>
                <div style="margin-top:10px; font-size:0.8rem;">Waiting for candle closures (1H / 4H)</div>
            </div>
            {% endif %}
        </div>

        <div>
            <h2>SYSTEM LOGS (Breakout Fast & Slow)</h2>
            <div class="log-box">{{ logs }}</div>
        </div>

    </div>
</body>
</html>
"""

# --- BACKEND LOGIC ---

def get_sys_stats():
    try:
        load = os.getloadavg()[0]
        out = subprocess.check_output("free -m", shell=True).decode()
        lines = out.split('\n')
        mem_line = [x for x in lines[1].split() if x]
        total_mem = int(mem_line[1])
        used_mem = int(mem_line[2])
        ram_pct = int((used_mem / total_mem) * 100)
        
        cpu_col = '#da3633' if load > 2.0 else '#d29922' if load > 1.0 else '#2ea043'
        ram_col = '#da3633' if ram_pct > 90 else '#d29922' if ram_pct > 75 else '#2ea043'

        return {'cpu': f"{load:.2f}", 'ram': f"{ram_pct}%", 'cpu_color': cpu_col, 'ram_color': ram_col}
    except:
        return {'cpu': 'ERR', 'ram': 'ERR', 'cpu_color':'', 'ram_color':''}

def get_positions_combined():
    positions = []
    
    # Buscamos todos los estados de Breakout
    breakout_files = glob.glob(os.path.join(BREAKOUT_DIR, "state_*.json"))
    
    for file_path in breakout_files:
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
                
                # Solo procesamos si está IN_POSITION
                if data.get('status') == 'IN_POSITION':
                    filename = os.path.basename(file_path)
                    # Reconstruir simbolo (state_SOL_USDT.json -> SOL/USDT)
                    symbol_raw = filename.replace("state_", "").replace(".json", "").replace("_", "/")
                    
                    # Determinar si es FAST o SLOW
                    engine_type = "FAST" if symbol_raw in PAIRS_FAST else "SLOW"
                    
                    positions.append({
                        'type': engine_type,
                        'symbol': symbol_raw,
                        'side': 'LONG', # Breakout es Long Only por ahora
                        'entry': data.get('entry_price'),
                        'sl': f"{data.get('stop_loss'):.4f}",
                        'tp': f"{data.get('tp_partial'):.4f}",
                        'trailing': data.get('trailing_active', False)
                    })
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

    return positions

def get_logs():
    try:
        # Traemos logs de AMBOS servicios
        service_flags = " ".join([f"-u {s}" for s in SERVICES])
        # Filtramos un poco para limpiar ruido de systemd
        cmd = f"journalctl {service_flags} -n 60 --no-pager"
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