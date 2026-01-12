# Usamos una imagen de Python ligera pero robusta
FROM python:3.10-slim

# Evita archivos basura y fuerza que los logs salgan inmediatemente (vital para ver el bot en vivo)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Directorio de trabajo
WORKDIR /app

# Instalar dependencias del sistema necesarias para compilar Pandas/Numpy
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar
COPY requirements.txt .
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todo el código del repositorio
COPY . .

# Comando de inicio
# Ejecuta el módulo principal. Asegúrate de que esta sea la ruta correcta a tu bot principal.
CMD ["python", "bots/breakout/main_breakout.py"]