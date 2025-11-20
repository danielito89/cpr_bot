# Deploying dual test v2 - CPRBot (v90) - Bot de Trading Multi-Par para Binance Futures

CPRBot es un sistema de trading algorítmico avanzado, modular y totalmente asíncrono, diseñado para operar múltiples pares simultáneamente en Binance Futures.

Utiliza una estrategia híbrida que combina **Pivotes Camarilla** y **CPR (Central Pivot Range)** con filtros de tendencia (EMA), volumen (Mediana de USDT) y confirmación de velas.

## ⚠️ Advertencia de Riesgo

**ESTE SOFTWARE ES PARA FINES EDUCATIVOS. ÚSELO BAJO SU PROPIO RIESGO.**
El trading de futuros conlleva un alto riesgo de pérdida de capital.
* **Estado:** Probado en Mainnet (v81).
* **Recomendación:** Inicie siempre con el mínimo apalancamiento y capital (`investment_pct=0.01`, `leverage=3`) hasta familiarizarse con el sistema.

---

## 🚀 Novedades en v81: Arquitectura Dinámica

La versión v81 introduce un **Orquestador Central** que permite:
* **Multi-Par Real:** Operar BTC, ETH, SOL, y cualquier otro par simultáneamente.
* **Gestión Dinámica:** Iniciar (`/start`) y detener (`/stop`) bots específicos desde Telegram sin reiniciar el servidor.
* **Eficiencia de Recursos:** Los bots detenidos liberan memoria RAM completamente, ideal para servidores pequeños (como AWS Lightsail).
* **Arquitectura Modular:** Código separado en `bot_core` (lógica pura) y `telegram` (comunicación).

---

## ⚙️ Estrategia y Gestión de Riesgo

El bot ejecuta una estrategia validada estadísticamente (Profit Factor > 1.6 en backtests de 6 meses):

### Entradas
1.  **Niveles Clave:** Busca rupturas en **H4/L4** (Breakout) o reversiones en **L3/H3** (Rango).
2.  **Filtro de Tendencia:** Usa una **EMA 20** para filtrar operaciones a favor de la tendencia en breakouts.
3.  **Filtro de Volumen:** Calcula la **Mediana de Volumen (USDT)** de los últimos 60 minutos. Solo opera si el volumen actual supera esa mediana por un factor (x1.3).
4.  **Confirmación de Vela:** Exige que la vela de señal tenga el color correcto (Verde para Long, Roja para Short).

### Salidas y Riesgo
* **Stop-Loss a Break-Even:** Mueve automáticamente el SL a la entrada al tocar el **TP2**.
* **Time Stop (12h):** Cierra operaciones de Rango si no han evolucionado favorablemente después de 12 horas.
* **Protección de Balance:** Pausa el trading si el PnL diario alcanza un límite negativo predefinido (15%).

---

## 🛠️ Instalación

### 1. Requisitos
* Python 3.10+
* Servidor Linux (Ubuntu recomendado)
* Cuenta de Binance Futures

### 2. Instalación
```bash
# Clonar repositorio
git clone [URL_DEL_REPO]
cd cpr_bot

# Crear entorno virtual
python3 -m venv venv
source venv/bin/activate

# Instalar dependencias
pip install --upgrade pip setuptools wheel
pip install python-binance httpx tenacity "pandas<2.2"


3. Configuración del Servicio (Systemd)
Edite el archivo cpr_bot.service:

[Service]
WorkingDirectory=/ruta/a/cpr_bot/cpr_bot_v81
ExecStart=/ruta/a/cpr_bot/venv/bin/python /ruta/a/cpr_bot/cpr_bot_v81/main_v81.py
Environment="BINANCE_API_KEY=TU_API_KEY"
Environment="BINANCE_SECRET_KEY=TU_SECRET_KEY"
Environment="TELEGRAM_BOT_TOKEN=TU_TOKEN"
Environment="TELEGRAM_CHAT_ID=TU_CHAT_ID"
Environment="TESTNET_MODE=false"

4. Seguridad en Binance
Para habilitar futuros, debe añadir la IP Estática de su servidor a la lista blanca de la API de Binance.

Permisos requeridos: Enable Reading, Enable Futures.

NO habilitar: Enable Withdrawals.

🤖 Comandos de Telegram (Orquestador)
El bot se controla 100% desde Telegram. No necesita acceder a la terminal.

Gestión de Pares
/start SIMBOLO - Inicia un nuevo bot para ese par (ej. /start SOLUSDT).

/stop SIMBOLO - Detiene el bot y libera memoria (ej. /stop ETHUSDT).

/list - Muestra todos los pares activos actualmente.

Monitoreo
/status - Muestra el estado (PnL, Posición, Indicadores) de todos los bots activos.

/status SIMBOLO - Muestra detalles de un par específico.

/pivots - Muestra los niveles Camarilla/CPR de todos los pares activos.

Control Global
/pausar - Pausa la búsqueda de nuevas entradas en todos los bots (mantiene gestión de posiciones abiertas).

/resumir - Reanuda la búsqueda de entradas.

/cerrar SIMBOLO - Cierre de Emergencia: Cierra la posición de ese par a mercado inmediatamente.

/restart - Reinicia el proceso del Orquestador completo.

📂 Estructura del Proyecto
Plaintext

cpr_bot_v81/
├── main_v81.py           # Orquestador principal (Entrypoint)
├── bot_core/             # Módulos de lógica pura
│   ├── symbol_strategy.py # Clase que instancia cada bot individual
│   ├── risk.py           # Lógica de entradas, salidas y filtros
│   ├── orders.py         # Ejecución de órdenes en Binance
│   ├── state.py          # Gestión de persistencia (JSON)
│   ├── pivots.py         # Cálculos matemáticos de niveles
│   ├── indicators.py     # Cálculos de EMA, ATR, Volumen
│   └── streams.py        # Gestión de WebSockets (Klines y User Data)
└── telegram/
    └── handler.py        # Interfaz de chat y comandos
