# CPRBot (v70) - Bot de Trading para Binance Futures

Este es un bot de trading algorítmico, totalmente asíncrono, diseñado para operar en Binance Futures. Utiliza una estrategia híbrida que combina Pivotes Camarilla y CPR (Central Pivot Range) con filtros de tendencia (EMA) y volumen (Mediana de USDT).

## ⚠️ Advertencia de Riesgo Fundamental

**NO ES UN CONSEJO FINANCIERO. ÚSELO BAJO SU PROPIO RIESGO.**
El trading de futuros es extremadamente arriesgado y puede resultar en la pérdida total de su capital. Este software se proporciona "tal cual", sin garantías de ningún tipo.

Se recomienda encarecidamente:
1.  Probar exhaustivamente en **Testnet** (aunque los datos de volumen no son fiables).
2.  Ejecutar la validación con el `backtester.py` incluido.
3.  Si decide operar en **Mainnet (dinero real)**, comience con los parámetros de riesgo más bajos posibles (`investment_pct = 0.01`, `leverage = 3`) y monitoree de cerca.

---

## ⚙️ Características Principales

* **100% Asíncrono:** Construido con `asyncio`, `httpx` y `python-binance` para un alto rendimiento y bajo consumo de recursos.
* **Estrategia Híbrida:** Reacciona tanto a escenarios de rango (L3/H3) como de ruptura (L4/H4) en los pivotes Camarilla.
* **Filtros de Estrategia:**
    * **Filtro de Tendencia (EMA 20):** Se aplica *solo* a las operaciones de breakout (L4/H4) para operar a favor de la tendencia.
    * **Filtro de Volumen (Mediana de USDT):** Protege contra entradas sin "convicción". Utiliza la **mediana** del volumen en USDT de 1m (últimos 60p) para ser robusto contra los *glitches* y valores atípicos.
* **Gestión de Riesgo Avanzada:**
    * **Stop-Loss a Break-Even:** Mueve automáticamente el SL al precio de entrada después de que se alcanza el **TP2**.
    * **Stop por Tiempo (Time Stop):** Cierra automáticamente las operaciones de *rango* (L3/H3) si no se han movido a BE después de 6 horas.
    * **Límite de Pérdida Diaria:** Pausa la apertura de nuevas operaciones si el PnL del día alcanza un umbral negativo.
* **Persistencia de Estado:** Guarda el estado completo del bot (`bot_state_v65.json`) de forma atómica, permitiendo que el bot se reinicie y continúe gestionando posiciones abiertas.
* **Control Total por Telegram:** Permite el monitoreo y control en tiempo real a través de comandos de bot.

---

## 🛠️ Instalación y Configuración

El bot está diseñado para correr como un servicio `systemd` en un servidor Linux (ej. Ubuntu en AWS Lightsail).

### 1. Requisitos Previos

* Un servidor Linux (se recomienda Ubuntu 22.04).
* Python 3.10 o superior.
* Una cuenta de Binance Futures (Mainnet).

### 2. Pasos de Instalación

1.  Clonar el repositorio:
    ```bash
    git clone [URL_DE_TU_REPOSITORIO]
    cd cpr_bot
    ```

2.  Crear y activar un entorno virtual (venv):
    ```bash
    python3.10 -m venv venv
    source venv/bin/activate
    ```

3.  Instalar las dependencias:
    ```bash
    # (Asegúrate de tener python3.10-dev y build-essential si la compilación falla)
    # sudo apt install python3.10-dev build-essential
    
    pip install --upgrade pip setuptools wheel
    pip install python-binance httpx tenacity "pandas<2.2"
    ```

### 3. Configuración del Servicio

El bot se ejecuta como un servicio `systemd` para asegurar que corra 24/7 y se reinicie automáticamente.

1.  Edita el archivo de servicio `cpr_bot.service` para asegurarte de que los nombres de archivo coincidan con la última versión (ej. `prod_bot_v65.py`).

    ```ini
    [Unit]
    Description=CPR Trading Bot Service v65
    After=network.target
    
    [Service]
    Type=simple
    User=ubuntu
    WorkingDirectory=/home/ubuntu/cpr_bot
    
    # Asegúrate de que esta ruta apunte a tu script v65
    ExecStart=/home/ubuntu/cpr_bot/venv/bin/python /home/ubuntu/cpr_bot/prod_bot_v65.py
    
    # --- ¡VARIABLES DE ENTORNO CRÍTICAS! ---
    # Claves de MAINNET
    Environment="BINANCE_API_KEY=TU_CLAVE_API_MAINNET"
    Environment="BINANCE_SECRET_KEY=TU_SECRETO_API_MAINNET"
    
    # Claves de Telegram
    Environment="TELEGRAM_BOT_TOKEN=TU_TOKEN_DE_TELEGRAM"
    Environment="TELEGRAM_CHAT_ID=TU_ID_DE_CHAT_NUMERICO"
    
    # Configuración del Bot
    Environment="TESTNET_MODE=false" # ¡Poner en 'false' para Mainnet!
    Environment="DAILY_LOSS_LIMIT_PCT=5.0" # 5%
    
    Environment="PYTHONUNBUFFERED=1" 
    Restart=always 
    RestartSec=10
    
    [Install]
    WantedBy=multi-user.target
    ```

2.  Copia el archivo al directorio de `systemd`:
    ```bash
    sudo cp cpr_bot.service /etc/systemd/system/cpr_bot.service
    ```

### 4. Configuración de Seguridad de Binance (Obligatorio)

La API de Mainnet **NO** funcionará si no haces esto:

1.  **Obtén la IP Estática** de tu servidor (en Lightsail, crea una "Static IP" y asóciala).
2.  **Ve a Binance > Gestión de API**.
3.  Crea una nueva clave de API.
4.  Selecciona **"Restringir el acceso a direcciones IP fiables"**.
5.  Pega la IP estática de tu servidor en la lista blanca.
6.  **Habilita Permisos:** Asegúrate de que *solo* estén marcadas `[X] Habilitar lectura` y `[X] Habilitar futuros`.
7.  **IMPORTANTE:** Asegúrate de que `[ ] Habilitar Retiros` esté **DESMARCADO**.

---

## 🚀 Uso

Una vez configurado el archivo `.service`:

1.  **Recargar Systemd:**
    ```bash
    sudo systemctl daemon-reload
    ```

2.  **Iniciar el Bot:**
    ```bash
    sudo systemctl start cpr_bot.service
    ```

3.  **Monitorear Logs en Vivo:**
    ```bash
    journalctl -u cpr_bot.service -f
    ```

4.  **Habilitar Auto-arranque** (para que el bot se inicie si el servidor se reinicia):
    ```bash
    sudo systemctl enable cpr_bot.service
    ```

---

## 🤖 Comandos de Telegram

Puedes controlar el bot en tiempo real desde el chat de Telegram que configuraste:

* `/status` - Muestra un informe completo: estado (activo/pausado), PnL del día, indicadores actuales y detalles de la posición abierta.
* `/pivots` - Muestra los niveles de pivote Camarilla (H1-L6) y CPR del día.
* `/pausar` - Pausa el bot. No buscará *nuevas* entradas. La gestión de posiciones activas continúa.
* `/resumir` - Reanuda la búsqueda de nuevas entradas.
* `/cerrar` - Cierra la posición actualmente abierta a precio de mercado. (¡Comando de emergencia!).
* `/forzar_indicadores` - Fuerza un recálculo inmediato de EMA, ATR y Mediana de Volumen.
* `/forzar_pivotes` - Fuerza un recálculo inmediato de los pivotes diarios.
* `/limit` - Muestra el límite de pérdida diaria configurado (%).
* `/restart` - Apaga y reinicia el bot de forma segura (systemd lo reiniciará).

---

## 📈 Backtesting

El repositorio incluye `download_data.py` y `backtester.py` para validar la estrategia.

1.  **Instalar Dependencias:**
    ```bash
    source venv/bin/activate
    # (Asegúrate de haber instalado python3.10-dev build-essential)
    pip install "pandas<2.2"
    ```

2.  **Descargar Datos Históricos:**
    *Aviso: Este proceso usa las claves de Mainnet, tarda mucho (horas) y consume mucha RAM (requiere `swap` en servidores pequeños).*
    ```bash
    # (Modifica START_DATE en el script si quieres menos datos)
    BINANCE_API_KEY="..." BINANCE_SECRET_KEY="..." python download_data.py
    ```

3.  **Ejecutar el Backtest:**
    ```bash
    python backtester.py
    ```
    El script imprimirá un resumen de resultados (PnL Neto, Win Rate, etc.) y guardará un CSV (`backtest_results_v65.csv`) con cada trade.

4.  **Optimizar:**
    Abre `backtester.py` y edita los parámetros en el **"Bloque 1: Configuración"** (ej. `EMA_PERIOD`, `VOLUME_FACTOR`) para encontrar la configuración más rentable.
