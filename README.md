Trading bot para Binance Futures (testnet)

1. Estrategia:
La estrategia del bot es un modelo híbrido que combina Pivotes Camarilla con un filtro de Media Móvil Exponencial (EMA) y un filtro de Volumen.
Intenta operar de dos maneras diferentes, dependiendo de dónde se encuentre el precio en relación con los niveles de Camarilla:

Operativa de Rango (Mean Reversion):

Señal Larga: Si el precio cae al nivel L3 (soporte de rango) Y el precio está por encima de la EMA(50) Y el volumen es alto, el bot compra, esperando que el precio "rebote" hacia el centro (P).

Señal Corta: Si el precio sube al nivel H3 (resistencia de rango) Y el precio está por debajo de la EMA(50) Y el volumen es alto, el bot vende, esperando que el precio "rebote" hacia el centro (P).

Operativa de Breakout (Ruptura):

Señal Larga: Si el precio supera el nivel H4 (ruptura alcista) Y el precio está por encima de la EMA(50) Y el volumen es alto, el bot compra, esperando que la tendencia continúe.

Señal Corta: Si el precio rompe el nivel L4 (ruptura bajista) Y el precio está por debajo de la EMA(50) Y el volumen es alto, el bot vende, esperando que la tendencia continúe.

En ambos casos, la EMA(50) actúa como un filtro de tendencia general: el bot solo buscará largos si el precio está por encima de la media móvil y solo buscará cortos si está por debajo.

¿Ajusta por Volumen?
Sí, pero más que "ajustar", lo utiliza como un filtro de confirmación crucial. El bot no entra en ninguna operación a menos que el volumen confirme el movimiento.

Así es como funciona en el código:

Cálculo de Volumen Promedio: El bot calcula el volumen promedio de las últimas 20 velas de 1 hora (_get_avg_volume_1h).

Factor de Volumen: Tiene un multiplicador (volume_factor, por defecto 1.5).

Confirmación: Al cerrarse cada vela de 1 minuto, comprueba: volume_confirmed = current_volume > (avg_vol * self.volume_factor)

Todas las 4 señales de entrada (Rango Largo/Corto, Breakout Largo/Corto) requieren que volume_confirmed sea True para poder ejecutarse.

2. Seguridad
La seguridad del bot es buena y sigue las mejores prácticas modernas para un proyecto de este tipo.

Gestión de Secretos: No hay claves de API ni tokens de Telegram escritos en el código. Todo se carga de forma segura desde las variables de entorno (os.environ.get). Esto es lo más importante.

Autenticación de Comandos: El bot no obedece a cualquiera. En la función _handle_telegram_message, comprueba explícitamente que el chat_id del mensaje sea el mismo que el TELEGRAM_CHAT_ID configurado en el entorno. Si otra persona encuentra tu bot y le escribe, el bot ignorará el comando.

Comunicaciones Cifradas: Todas las conexiones, tanto a Binance como a Telegram, se realizan sobre HTTPS, por lo que el tráfico está cifrado.

3. Estabilidad
La estabilidad del bot es excelente y está diseñado para ser muy robusto.

Resiliencia de Red (Binance): El decorador @tenacity_retry_decorator_async se aplica a todas las llamadas críticas a la API de Binance (obtener klines, calcular pivotes, obtener balance, etc.). Si Binance da un error temporal o tu servidor tiene un micro-corte de red, el bot no se caerá; reintentará la llamada de forma inteligente (con espera exponencial) hasta 5 veces antes de fallar.

Reconexión de Websocket: El bucle principal async def run utiliza async with self.bsm.kline_socket(...). Este "context manager" de la librería de Binance está diseñado específicamente para manejar reconexiones automáticamente. Si la conexión del websocket se cae, la librería la reestablecerá por su cuenta, asegurando que el bot no se quede "ciego".

Guardado de Estado "Atómico": La función save_state primero escribe en un archivo temporal (.tmp) y solo cuando tiene éxito, lo mueve para reemplazar al archivo de estado principal. Esto previene que tu archivo bot_state_v55.json se corrompa si el bot se apaga o crashea justo en mitad de un guardado.

Apagado Limpio (Graceful Shutdown): El bot incluye manejadores de señales para SIGINT y SIGTERM. Cuando systemd (el gestor de servicios de tu servidor) le dice al bot que se detenga (sudo systemctl stop cpr_bot.service), el bot lo detecta, llama a await self.shutdown(), guarda su estado por última vez y se cierra limpiamente.

4. Consumo de Recursos
El consumo de recursos es extremadamente bajo.

CPU: El bot está construido sobre asyncio. Esto significa que pasa el 99.9% de su tiempo "dormido" (en estado await), sin consumir CPU. Solo se "despierta" en ráfagas de milisegundos para procesar un evento (un tick de websocket, una respuesta de Telegram) y se vuelve a dormir.

Memoria (RAM): El uso de memoria es muy bajo. No usa librerías pesadas como pandas. El estado que guarda en memoria (pivotes, indicadores, estado de posición) es un diccionario de Python muy pequeño.

Conclusión de Recursos: Este bot podría correr sin problemas en el servidor VPS más pequeño y barato que exista (como un t2.micro de AWS o similar) y aún le sobraría el 90% de los recursos.
