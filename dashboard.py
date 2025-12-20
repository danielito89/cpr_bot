from flask import Flask, render_template_string
import subprocess
import time

app = Flask(__name__)

# ---------------- CONFIGURACIÓN DE LOGS ----------------

# 1. Crash Bot (Rojo)
CMD_LOG_CRASH = "journalctl -u cpr_crash.service -n 20 --no-pager"

# 2. Golden Cross (Verde)
CMD_LOG_GOLDEN = "journalctl -u cpr_bot.service -n 20 --no-pager"

# 3. Scalper Pro V6.4 (Naranja - Nuevo)
# Nota: Usamos el nombre del servicio que creamos: scalper_pro.service
CMD_LOG_SCALP = "journalctl -u scalper_pro.service -n 20 --no-pager"

# ---------------- HTML TEMPLATE ----------------
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>CPR Command Center</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        body { background-color: #0d1117; color: #c9d1d9; font-family: 'Consolas', 'Monaco', monospace; padding: 20px; }
        .header { text-align: center; margin-bottom: 30px; border-bottom: 1px solid #30363d; padding-bottom: 20px; }
        .card { background: #161b22; border: 1px solid #30363d; padding: 20px; margin-bottom: 20px; border-radius: 6px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { margin-top: 0; border-bottom: 1px solid #30363d; padding-bottom: 10px; font-size: 1.1rem; display: flex; align-items: center; justify-content: space-between; }
        
        .status-dot { height: 12px; width: 12px; border-radius: 50%; display: inline-block; margin-right: 10px; }
        .green-dot { background-color: #2ea043; box-shadow: 0 0 8px #2ea043; }
        .red-dot { background-color: #da3633; box-shadow: 0 0 8px #da3633; }
        .orange-dot { background-color: #f6ae2d; box-shadow: 0 0 8px #f6ae2d; }
        
        pre { background: #0d1117; padding: 15px; border-radius: 4px; overflow-x: auto; font-size: 11px; color: #8b949e; border: 1px solid #30363d; white-space: pre-wrap; word-wrap: break-word; line-height: 1.4; }
        
        .footer { text-align: center; font-size: 0.8rem; color: #484f58; margin-top: 30px; }
        .tag { font-size: 0.7em; padding: 2px 6px; border-radius: 4px; background: #21262d; border: 1px solid #30363d; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🏥 CPR MONITOR <span style="color:#58a6ff;">LITE</span></h1>
        <div style="font-size: 0.9em; color: #8b949e;">Sistema de Gestión de Bots</div>
    </div>

    <div class="card" style="border-left: 4px solid #f6ae2d;">
        <h2>
            <span><span class="status-dot orange-dot"></span> SCALPER PRO V6.4</span>
            <span class="tag">ACTIVE TRADING</span>
        </h2>
        <pre>{{ log_scalp }}</pre>
    </div>

    <div class="card" style="border-left: 4px solid #da3633;">
        <h2>
            <span><span class="status-dot red-dot"></span> CRASH BOT</span>
            <span class="tag">HEDGING</span>
        </h2>
        <pre>{{ log_crash }}</pre>
    </div>

    <div class="card" style="border-left: 4px solid #2ea043;">
        <h2>
            <span><span class="status-dot green-dot"></span> GOLDEN CROSS BOT</span>
            <span class="tag">TREND</span>
        </h2>
        <pre>{{ log_golden }}</pre>
    </div>
    
    <div class="footer">
        Server Time: {{ last_update }} | Refresh: 30s
    </div>
</body>
</html>
"""

# ---------------- LÓGICA BACKEND ----------------

def get_logs(cmd):
    try:
        # Ejecutamos el comando de sistema para leer logs
        output = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, timeout=2)
        decoded = output.decode('utf-8').strip()
        if not decoded:
            return "⚠️ El servicio está corriendo pero no hay logs recientes."
        return decoded
    except subprocess.TimeoutExpired:
        return "⚠️ Timeout leyendo logs (Sistema ocupado)"
    except subprocess.CalledProcessError:
        return "⚠️ Servicio detenido o no encontrado."
    except Exception as e:
        return f"⚠️ Error: {str(e)}"

@app.route('/')
def index():
    # Solo leemos logs, nada de API externa (Super Rápido)
    log_scalp = get_logs(CMD_LOG_SCALP)
    log_crash = get_logs(CMD_LOG_CRASH)
    log_golden = get_logs(CMD_LOG_GOLDEN)
    
    now = time.strftime("%H:%M:%S UTC")
    
    return render_template_string(HTML, 
                                  log_scalp=log_scalp, 
                                  log_crash=log_crash, 
                                  log_golden=log_golden,
                                  last_update=now)

if __name__ == '__main__':
    # Escucha en todas las interfaces para que entres desde tu celular/PC
    app.run(host='0.0.0.0', port=5000)