from flask import Flask, render_template_string
import subprocess
import json
import os
import time
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN ---
# Ajusta la ruta si tu usuario es diferente (ej: /home/orangepi/...)
BASE_PATH = "/home/ubuntu/bot_cpr" 
STATE_FILE = os.path.join(BASE_PATH, "scalper_pro", "hydra_state.json")
SERVICE_NAME = "scalper_pro.service"

# --- HTML TEMPLATE (DARK MODE PRO) ---
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>HYDRA COMMAND CENTER</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="10">
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { --bg: #0d1117; --card: #161b22; --border: #30363d; --text: #c9d1d9; --accent: #58a6ff; --green: #2ea043; --red: #da3633; --orange: #d29922; }
        body { background-color: var(--bg); color: var(--text); font-family: 'JetBrains Mono', monospace; padding: 20px; margin: 0; }
        
        /* Grid Layout */
        .container { max-width: 1200px; margin: 0 auto; display: grid; gap: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 15px; }
        
        /* Stats Bar */
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
        .stat-card { background: var(--card); border: 1px solid var(--border); padding: 15px; border-radius: 6px; text-align: center; }
        .stat-value { font-size: 1.5rem; font-weight: bold; color: var(--accent); }
        .stat-label { font-size: 0.8rem; color: #8b949e; text-transform: uppercase; }
        .warning { color: var(--orange); } .danger { color: var(--red); }

        /* Active Positions Table */
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
        th { color: #8b949e; font-size: 0.8rem; text-transform: uppercase; }
        .badge { padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
        .badge-long { background: rgba(46, 160, 67, 0.15); color: var(--green); border: 1px solid var(--green); }
        .badge-short { background: rgba(218, 54, 51, 0.15); color: var(--red); border: 1px solid var(--red); }

        /* Logs */
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
                <h1 style="margin:0; font-size:1.5rem;">🐲 HYDRA <span style="color:var(--accent)">COMMAND CENTER</span></h1>
                <div style="font-size:0.8rem; color:#8b949e; margin-top:5px;">System Integrity: ONLINE | Mode: MULTIPAIR</div>
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
                <div class="stat-value {{ swap_color }}">{{ swap_usage }}</div>
                <div class="stat-label">SWAP USAGE</div>
            </div>
            <div class="stat-card">
                <div class="stat-value" style="color:var(--text)">{{ active_positions_count }}</div>
                <div class="stat-label">ACTIVE POSITIONS</div>
            </div>
        </div>

        <div style="background:var(--card); border:1px solid var(--border); padding:20px; border-radius:6px;">
            <h2><span class="dot dot-green"></span> LIVE POSITIONS (FROM STATE)</h2>
            {% if positions %}
            <table>
                <thead>
                    <tr>
                        <th>PAIR</th>
                        <th>SIDE</th>
                        <th>ENTRY</th>
                        <th>STOP LOSS</th>
                        <th>DURATION</th>
                    </tr>
                </thead>
                <tbody>
                    {% for p in positions %}
                    <tr>
                        <td style="font-weight:bold; color:#fff;">{{ p.symbol }}</td>
                        <td><span class="badge {{ 'badge-long' if p.side == 'LONG' else 'badge-short' }}">{{ p.side }}</span></td>
                        <td>${{ p.entry }}</td>
                        <td>${{ p.sl }}</td>
                        <td>{{ p.bars }} bars</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <div style="padding:20px; text-align:center; color:#8b949e; font-style:italic;">
                Scanning markets... No active positions.
            </div>
            {% endif %}
        </div>

        <div>
            <h2>TERMINAL OUTPUT</h2>
            <div class="log-box">{{ logs }}</div>
        </div>

    </div>
</body>
</html>
"""

# --- BACKEND LOGIC ---

def get_sys_stats():
    """Obtiene métricas del servidor sin dependencias externas"""
    try:
        # Load Avg
        load = os.getloadavg()[0]
        
        # RAM & Swap (Parsing free -m)
        out = subprocess.check_output("free -m", shell=True).decode()
        lines = out.split('\n')
        
        # Memoria
        mem_line = [x for x in lines[1].split() if x]
        total_mem = int(mem_line[1])
        used_mem = int(mem_line[2])
        ram_pct = int((used_mem / total_mem) * 100)
        
        # Swap
        swap_line = [x for x in lines[2].split() if x]
        total_swap = int(swap_line[1])
        used_swap = int(swap_line[2])
        swap_pct = int((used_swap / total_swap) * 100) if total_swap > 0 else 0
        
        return {
            'cpu': f"{load:.2f}",
            'cpu_color': 'danger' if load > 2.0 else 'warning' if load > 1.0 else '',
            'ram': f"{ram_pct}%",
            'ram_color': 'danger' if ram_pct > 90 else 'warning' if ram_pct > 75 else '',
            'swap': f"{swap_pct}%",
            'swap_color': 'danger' if swap_pct > 50 else 'warning' if swap_pct > 20 else ''
        }
    except:
        return {'cpu': 'ERR', 'ram': 'ERR', 'swap': 'ERR', 'cpu_color':'', 'ram_color':'', 'swap_color':''}

def get_positions_from_state():
    """Lee el archivo JSON del bot para ver posiciones reales"""
    positions = []
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                for symbol, info in data.items():
                    if info.get('in_position'):
                        positions.append({
                            'symbol': symbol,
                            'side': info.get('side', 'UNK'),
                            'entry': info.get('entry_price'),
                            'sl': f"{info.get('stop_loss'):.4f}", # Format price
                            'bars': info.get('bars_held')
                        })
        except: pass
    return positions

def get_logs():
    try:
        cmd = f"journalctl -u {SERVICE_NAME} -n 30 --no-pager"
        return subprocess.check_output(cmd, shell=True).decode().strip()
    except: return "Error reading logs."

@app.route('/')
def index():
    stats = get_sys_stats()
    positions = get_positions_from_state()
    logs = get_logs()
    
    return render_template_string(HTML, 
                                  cpu_usage=stats['cpu'], cpu_color=stats['cpu_color'],
                                  ram_usage=stats['ram'], ram_color=stats['ram_color'],
                                  swap_usage=stats['swap'], swap_color=stats['swap_color'],
                                  active_positions_count=len(positions),
                                  positions=positions,
                                  logs=logs,
                                  time_now=datetime.utcnow().strftime('%H:%M:%S'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)