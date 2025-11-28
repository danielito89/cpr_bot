# 🚀 CPRBot (v99) - Sistema de Trading Algorítmico Multi-Par

**CPRBot** es una infraestructura de trading de alta frecuencia diseñada para **Binance Futures**, optimizada para operar múltiples pares (BTC, ETH, BNB, SOL) simultáneamente mediante una arquitectura **Multiplex** (un solo socket para todos los datos), lo que permite un consumo mínimo de recursos (ideal para AWS Lightsail).

---

## 🧠 Estrategia y Lógica (Híbrida v99)

El bot no predice el futuro; reacciona a la **Volatilidad** y la **Estructura de Mercado** en velas de 1 minuto.

### 1. Motor de Entradas (Pivotes Camarilla + CPR)
El bot evalúa cada cierre de vela (1m) buscando la alineación de 4 factores:
* **Niveles Clave:**
    * **Breakout (Tendencia):** Ruptura de **H4** (Long) o **L4** (Short). *Prioridad Alta.*
    * **Rango (Reversión):** Rebote en **L3** (Long) o **H3** (Short). *Secundario.*
* **Filtro de Volumen (Smart):** El volumen debe ser superior a **1.1x** la Mediana de los últimos 60 minutos.
* **Filtro de Tendencia:** EMA 20 (1H) actúa como brújula. Solo opera a favor de la corriente.
* **Confirmación de Vela:** La vela de señal debe tener el color de la dirección del trade.

### 2. Gestión de Salidas (Dinámicas)
* **ETH/SOL (Runners):** Usan **Trailing Stop** agresivo (Trigger 1.25 ATR / Distancia 1.0 ATR) para capturar "Home Runs" y tendencias largas.
* **BTC/BNB (Snipers):** Usan **Take Profit Fijo** (1.25 ATR) o Trailing conservador para asegurar ganancias en mercados con retrocesos profundos.
* **Rango:** TPs estructurales en niveles Camarilla (L1, H1, H3).

---

## 🛡️ Risk Manager v99 (Defensa en Profundidad)

El corazón del sistema es su gestor de riesgo centralizado ("El Portero"):

1.  **Zombie Killer & State First:**
    * Detecta automáticamente si una posición se cerró en Binance (`qty < 0.0001`) y limpia la memoria local inmediatamente.
    * Limpia órdenes pendientes ("basura") tras cada cierre.
2.  **Smart Cooldown:**
    * ✅ **Ganancia:** 0 minutos de espera (Re-entrada inmediata para aprovechar rachas).
    * ❌ **Pérdida:** 15 minutos de espera (Protección contra turbulencia).
    * ⏳ **Neutro:** 5 minutos.
3.  **Smart Schedule (Filtro de Calendario):**
    * 🚫 **Sábados:** Bloqueado (Bajo rendimiento estadístico).
    * 🚫 **Horas Tóxicas:** 04, 10, 13 UTC (Bloqueadas por baja efectividad).
4.  **Protección de Capital:**
    * **Nuclear Stop Loss:** Cierra el 100% de la posición en el exchange (`closePosition=true`).
    * **Límite Diario:** Apaga el bot si se pierde el **15%** del balance diario.
    * **Techo de Posición:** Limita el tamaño máximo por trade (ej. $50,000) para evitar problemas de liquidez.

---

## 🛠️ Instalación y Despliegue

### Requisitos
* Python 3.10+
* Servidor Linux (Ubuntu/Debian/Armbian)
* Cuenta Binance Futures (API Key con permisos de Futuros)

### 1. Clonar y Preparar Entorno
```bash
# Clonar repositorio
git clone [https://github.com/TU_USUARIO/bot_cpr.git](https://github.com/TU_USUARIO/bot_cpr.git)
cd bot_cpr

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

2. Configuración Segura (.env)
Crea un archivo .env en la carpeta del bot (cpr_bot_v90/) con tus credenciales. NO subir a GitHub.

BINANCE_API_KEY=tu_api_key
BINANCE_SECRET_KEY=tu_secret_key
TELEGRAM_BOT_TOKEN=tu_bot_token
TELEGRAM_CHAT_ID=tu_chat_id
TESTNET_MODE=false
DAILY_LOSS_LIMIT_PCT=15.0

3. Ejecución como Servicio (Producción 24/7)

Configura systemd para que el bot corra en segundo plano y reinicie automáticamente.

sudo nano /etc/systemd/system/cpr_bot.service

Pega la configuración (ajusta rutas):

Description=CPR Trading Bot v99
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/bot_cpr/cpr_bot_v90
ExecStart=/home/ubuntu/bot_cpr/venv/bin/python /home/ubuntu/bot_cpr/cpr_bot_v90/main_v90.py
EnvironmentFile=/home/ubuntu/bot_cpr/cpr_bot_v90/.env
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target

Activar:

sudo systemctl daemon-reload
sudo systemctl enable cpr_bot.service
sudo systemctl start cpr_bot.service

🤖 Comandos de Telegram

Gestiona tu flota de bots sin tocar la terminal SSH.

/status Informe ejecutivo. Estado de conexión, PnL de posiciones abiertas y valor de indicadores.
/start BTCUSDT Inicia un nuevo hilo de trading para el par especificado.
/stop BTCUSDT Detiene el hilo y libera memoria RAM.
/pivots Muestra los niveles matemáticos del día y el tipo de estructura (Rango/Tendencia).
/reset BTCUSDT Emergencia: Fuerza el borrado de la memoria local del bot y resincroniza con Binance.
/cerrar BTCUSDT Cierra inmediatamente la posición a precio de mercado.
/list Lista los bots activos.

🧪 Backtesting y Laboratorio

El repositorio incluye un motor de simulación profesional (backtester_v5.py) que replica la lógica del RiskManager v99.
Características del Backtester:
- Fricción Real: Simula comisiones y Slippage.
- No Look-ahead: Desplaza indicadores para usar solo datos cerrados.
- Risk Aware: Respeta horarios prohibidos, cooldowns y límites de pérdida igual que el bot en vivo. 

Cómo ejecutar un Backtest (en entorno de Laboratorio):
Descargar Datos:
 Edita download_data.py para elegir par y fechas
python download_data.py
Correr Simulación:
 Edita backtester_v5.py para ajustar parámetros
python backtester_v5.py

Analizar Horarios (Opcional):
python analyze_hours.py

📂 Estructura del ProyectoPlaintextbot_cpr/
├── cpr_bot_v90/
│   ├── main_v90.py           # Orquestador (Entrypoint)
│   ├── bot_core/             # Núcleo Lógico
│   │   ├── risk.py           # Cerebro (Decisiones y Seguridad)
│   │   ├── orders.py         # Ejecución (Binance API)
│   │   ├── symbol_strategy.py # Gestión de Tareas de Fondo
│   │   ├── pivots.py         # Matemáticas (Camarilla/CPR)
│   │   ├── indicators.py     # Matemáticas (ATR/EMA/Vol)
│   │   └── state.py          # Persistencia JSON
│   ├── tg_services/          # Módulo Telegram
│   ├── data/                 # Estado (.json) y Logs de Trades (.csv)
│   ├── backtester_v5.py      # Simulador Profesional
│   └── ...
├── requirements.txt          # Dependencias
└── .github/workflows/        # CI/CD (Dual Deploy)


Sistema desarrollado con arquitectura escalable para alta disponibilidad y seguridad.