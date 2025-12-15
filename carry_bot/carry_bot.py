import ccxt
import time
import os
import logging
from dotenv import load_dotenv

# ======================================================
#  🏦 CPR CARRY BOT V2.0 - DELTA NEUTRAL CORE
# ======================================================

# --- CONFIGURACIÓN ---
TARGETS = {
    'BTC/USDT': {'min_size': 0.002}, # Ajustar a tu capital real
    'ETH/USDT': {'min_size': 0.02}   
}

# --- PARAMETROS DE ENTRADA/SALIDA (Histéresis) ---
ENTRY_THRESHOLD = 0.00001  # Entrar si Tasa > 0.01%
EXIT_THRESHOLD  = -0.0002 # Salir si Tasa < -0.02% (Tolera ruido negativo leve)

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - CARRY - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger()

def get_exchange():
    """Inicializa la conexión a Binance Futures"""
    load_dotenv()
    return ccxt.binance({
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_SECRET_KEY'),
        'enableRateLimit': True,
        'options': {'defaultType': 'future'} # Por defecto operamos futuros
    })

def check_spot_balance(exchange, symbol, required_qty):
    """
    CORRECCIÓN CRÍTICA #1: Verifica que exista el colateral en SPOT.
    Retorna True si hay fondos suficientes para cubrir el Short.
    """
    try:
        base_currency = symbol.split('/')[0] # 'BTC' de 'BTC/USDT'
        
        # Forzamos la lectura del balance SPOT (override del defaultType='future')
        spot_balance = exchange.fetch_balance({'type': 'spot'})
        
        available = float(spot_balance[base_currency]['free'])
        
        if available >= required_qty:
            return True
        else:
            logger.error(f"🚨 ALERTA DE SEGURIDAD: Spot insuficiente en {base_currency}. "
                         f"Tienes {available}, requieres {required_qty}. SHORT ABORTADO.")
            return False
            
    except Exception as e:
        logger.error(f"❌ Error verificando Spot Balance: {e}")
        return False

def run_carry_cycle(exchange):
    """Ciclo principal de lógica de negocio"""
    logger.info("--- INICIANDO ESCANEO DE RENTA FIJA (CORE) ---")
    
    try:
        # Cargar posiciones actuales de Futuros
        balance_f = exchange.fetch_balance()
        positions = balance_f['info']['positions']
        
        for symbol, config in TARGETS.items():
            # 1. Obtener Funding Rate Actual
            funding = exchange.fetch_funding_rate(symbol)
            current_rate = float(funding['fundingRate'])
            
            # 2. Buscar estado de la posición
            market = exchange.market(symbol)
            # Binance usa Symbols sin barra en la data cruda de posiciones (ej: BTCUSDT)
            raw_symbol = market['id'] 
            
            pos_info = next((p for p in positions if p['symbol'] == raw_symbol), None)
            amt = float(pos_info['positionAmt']) if pos_info else 0
            
            # Estamos en posición si tenemos Short (negativo)
            in_position = amt < 0 
            
            log_msg = f"📊 {symbol} | Rate: {current_rate*100:.4f}% | Pos: {amt}"
            
            # --- LÓGICA DE DECISIÓN ---
            
            # CASO A: ABRIR (Entrada)
            if not in_position and current_rate > ENTRY_THRESHOLD:
                logger.info(f"{log_msg} -> Oportunidad detectada.")
                
                # VERIFICACIÓN DE SEGURIDAD (SPOT)
                qty = config['min_size']
                if check_spot_balance(exchange, symbol, qty):
                    logger.info(f"✅ Spot verificado. Ejecutando SHORT 1x en {symbol}...")
                    try:
                        exchange.set_leverage(1, symbol)
                        exchange.create_market_sell_order(symbol, qty)
                        logger.info(f"🚀 ORDEN COMPLETADA: Short {qty} {symbol}")
                    except Exception as e:
                        logger.error(f"❌ Falló la orden de entrada: {e}")
                else:
                    logger.warning(f"⚠️ Se omitió entrada en {symbol} por falta de Spot.")

            # CASO B: CERRAR (Salida con Histéresis)
            elif in_position and current_rate < EXIT_THRESHOLD:
                logger.warning(f"{log_msg} -> Tasa muy negativa ({current_rate}). SALIDA DE EMERGENCIA.")
                try:
                    exchange.create_market_buy_order(symbol, abs(amt))
                    logger.info(f"🛑 POSICIÓN CERRADA en {symbol}. Renta Fija pausada.")
                except Exception as e:
                    logger.error(f"❌ Falló la orden de salida: {e}")
            
            # CASO C: MANTENER
            elif in_position:
                logger.info(f"{log_msg} -> Manteniendo (Cobrando Tasa 💰)")
            
            else:
                logger.info(f"{log_msg} -> Esperando oportunidad...")

    except Exception as e:
        logger.error(f"Error general en el ciclo: {e}")

# --- PUNTO DE ENTRADA ---
if __name__ == "__main__":
    # CORRECCIÓN #2: Instancia única fuera del loop
    try:
        bot_exchange = get_exchange()
        logger.info("🔌 Conexión a Exchange establecida exitosamente.")
        
        while True:
            run_carry_cycle(bot_exchange)
            logger.info("💤 Durmiendo 1 hora...")
            time.sleep(3600) 
            
    except Exception as e:
        logger.critical(f"🔥 Error fatal al iniciar: {e}")