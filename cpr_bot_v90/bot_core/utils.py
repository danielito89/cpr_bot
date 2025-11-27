import logging
from decimal import Decimal, ROUND_DOWN
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
from binance.exceptions import BinanceAPIException
from logging.handlers import RotatingFileHandler
import httpx
import json
import shutil
from decimal import Decimal

# --- CONSTANTES DE TRADING ---
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
ORDER_TYPE_MARKET = "MARKET"
STOP_MARKET = "STOP_MARKET"
TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"

# --- CABECERA CSV ---
CSV_HEADER = [
    "timestamp_utc", "entry_type", "side", "quantity", "entry_price", "mark_price_entry",
    "close_price_avg", "pnl", "pnl_percent_roi", "cpr_width", "atr_at_entry", "ema_filter"
]



def setup_logging(log_file):
    """Configura el logger para la aplicación."""
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    # (Aquí podríamos añadir el RotatingFileHandler si queremos)
    console = logging.StreamHandler()
    console.setFormatter(log_formatter)

    file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=3)
    file_handler.setFormatter(log_formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(console)

    if logger.hasHandlers():
        logger.handlers.clear()

    logger.addHandler(console)
    logger.addHandler(file_handler)

    # Silenciar librerías ruidosas
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)

    return logger

def tenacity_retry_decorator_async():
    """Decorador de reintentos para llamadas de API."""
    return retry(
        wait=wait_exponential(multiplier=1, min=1, max=20),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type((httpx.RequestError, BinanceAPIException)),
        reraise=True,
    )

def format_price(tick_size, p):
    """Formatea un precio según el tick_size del exchange."""
    try:
        if tick_size:
            return str(Decimal(str(p)).quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN))
    except Exception:
        pass
    return f"{float(p):.8f}"

def format_qty(step_size, q):
    """Formatea una cantidad según el step_size del exchange."""
    try:
        if step_size:
            return str(Decimal(str(q)).quantize(Decimal(str(step_size)), rounding=ROUND_DOWN))
    except Exception:
        pass
    return f"{float(q):.8f}"

def sanitize_for_json(data):
    """Convierte recursivamente Decimals y otros tipos no serializables a float."""
    if isinstance(data, dict):
        return {k: sanitize_for_json(v) for k, v in data.items()}
    if isinstance(data, list):
        return [sanitize_for_json(x) for x in data]
    if isinstance(data, Decimal):
        return float(data)
    # Añadir más tipos si es necesario
    return data

def atomic_save_json(data, file_path):
    """Guarda un diccionario a un archivo JSON de forma atómica."""
    tmp_path = file_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(sanitize_for_json(data), f, indent=2)
        shutil.move(tmp_path, file_path)
        logging.info("Estado guardado atómicamente.")
    except Exception as e:
        logging.error(f"Error guardando estado atómicamente: {e}")
