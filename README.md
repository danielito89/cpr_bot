Trading bot para Binance Futures (testnet)

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
