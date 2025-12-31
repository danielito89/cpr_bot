import ccxt
exchange = ccxt.binance({'options': {'defaultType': 'future'}})
exchange.load_markets()

# Buscar todo lo que tenga PEPE
print("Buscando PEPE en mercados de futuros...")
for symbol in exchange.markets:
    if 'PEPE' in symbol:
        print(f"Simbolo CCXT: {symbol} | ID Binance: {exchange.markets[symbol]['id']}")