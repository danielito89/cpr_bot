# 🚀 CPRBot (v90) - Sistema de Trading Multi-Par Institucional

**CPRBot** es un motor de trading algorítmico de alto rendimiento para **Binance Futures**, diseñado con una arquitectura asíncrona y modular.

A diferencia de los bots tradicionales que abren una conexión por moneda, CPRBot utiliza una arquitectura **Multiplex** (un solo túnel de datos para todos los pares), permitiendo operar múltiples mercados simultáneamente con un consumo mínimo de CPU y RAM (ideal para AWS Lightsail o Orange Pi).

---

## 🧠 Estrategia y Lógica (Validada v90.5)

El bot ejecuta una estrategia **Híbrida (Breakout + Rango)** optimizada mediante backtesting de 8 meses, buscando ineficiencias en niveles de Pivotes Camarilla y CPR.

### 1. Motor de Decisiones
El bot evalúa cada vela de **1 minuto** buscando la alineación perfecta de 4 factores:
* **Niveles Clave:** Ruptura de **H4/L4** (Prioridad) o Rebote en **L3/H3**.
* **Volumen Institucional:** El volumen debe superar la **Mediana de 60 periodos** multiplicada por un factor (x1.3).
* **Confirmación de Vela:** La vela de señal debe tener el color de la dirección del trade (Verde para Long, Roja para Short).
* **Tendencia (EMA 20):** Filtro de media móvil exponencial en 1H para operar a favor de la corriente.

### 2. Gestión de Riesgo Avanzada (RiskManager)
El sistema cuenta con un "Juez Central" que aprueba o rechaza cada operación antes de enviarla:
* **Smart Cooldown:**
    * ✅ Ganancia: **0 min** (Re-entrada inmediata para aprovechar rachas).
    * ❌ Pérdida: **15 min** (Protección contra mercados turbulentos).
    * ⏳ Neutro: **5 min**.
* **Trailing Stop:** Stop Loss dinámico que persigue el precio para asegurar ganancias en tendencias largas.
* **Time Stop (12h):** Cierre automático de operaciones de Rango que no evolucionan tras 12 horas.
* **Protección de Ruina:** Bloqueo total del día si se pierde el **15%** del capital diario.

---

## 🛠️ Instalación y Despliegue

### Requisitos Previos
* Python 3.10 o superior.
* Servidor Linux (Ubuntu/Debian/Armbian).
* Cuenta de Binance Futures.

### Paso 1: Clonar y Entorno
```bash
# Clonar el repositorio
git clone [URL_DE_TU_REPO] bot_cpr
cd bot_cpr

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

### Paso 2: Configuración Segura (.env)
Crea un archivo .env en la carpeta de la versión actual (ej. cpr_bot_v90/) para guardar tus claves. Nunca subas esto a GitHub.Ini, TOMLBINANCE_API_KEY=tu_api_key_real
BINANCE_SECRET_KEY=tu_secret_key_real
TELEGRAM_BOT_TOKEN=tu_token_telegram
TELEGRAM_CHAT_ID=tu_id_numerico
TESTNET_MODE=false
DAILY_LOSS_LIMIT_PCT=15.0


### Paso 3: Ejecución como Servicio (Systemd)
Para que el bot corra 24/7 y reinicie si falla:Edita el archivo de servicio: sudo nano /etc/systemd/system/cpr_bot.serviceAsegúrate de que apunte a tu carpeta bot_cpr y al archivo main_v90.py.
Activa el servicio: 
sudo systemctl daemon-reload
sudo systemctl enable cpr_bot.service
sudo systemctl start cpr_bot.service


🤖 Comandos de Telegram (Gestión Dinámica)
El bot se controla totalmente desde Telegram. Puedes añadir o quitar monedas sin reiniciar el servidor.
/start BTCUSDT Inicia un nuevo bot para BTC. Descarga datos y conecta Websockets al instante.
/stop ETHUSDTDetiene el bot de ETH y libera la memoria RAM.
/status Muestra un informe ejecutivo de todos los pares activos y su PnL actual.
/pivots Muestra los niveles Camarilla/CPR del día con análisis de estructura (Rango/Tendencia).
/list Lista qué pares se están operando actualmente.
/cerrar SOLUSDT Emergencia: Cierra la posición de SOL a mercado inmediatamente.
/reset BTCUSDT Técnico: Fuerza el reseteo de la memoria interna del bot (útil si hay desincronización).

🧪 Backtesting y Validación
El proyecto incluye un motor de backtesting profesional (backtester_v5.py) que simula:  
    Fricción Real: Comisiones (Entry/Exit) + Slippage.
    Lookahead Bias Free: Garantiza que el bot no "vea el futuro" al calcular indicadores.
    Risk Aware: El simulador respeta los límites de pérdida diaria y cooldowns del bot real.
    
Para correr un backtest (recomendado en un PC potente):
Bash# 1. Descargar datos históricos
python download_data.py
2. Ejecutar simulación
python backtester_v5.py


📂 Estructura del Proyecto
main_v90.py: Orquestador. Gestiona la conexión Multiplex y los hilos de cada par
bot_core/: Cerebro modular.
    risk.py: Lógica de decisión (Entradas/Salidas/Seguridad).
    symbol_strategy.py: Instancia pasiva que maneja el estado de una moneda.
    orders.py: Ejecución y formateo de órdenes.
    pivots.py / indicators.py: Matemática financiera.
telegram/: Gestión de comandos y notificaciones.
data/: Almacenamiento de estados (.json) y logs de operaciones (.csv).

Desarrollado con arquitectura escalable para alta disponibilidad.