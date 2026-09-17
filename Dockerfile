# Image de production : API FastAPI + interface statique + boucles d'envoi,
# un seul processus. Les données (SQLite, jeton Gmail, CV, exports, clé de
# session) vivent dans /data, un volume persistant monté par l'hébergeur.
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1

# lxml a besoin des bibliothèques XML ; curl sert au bilan de santé.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libxml2 libxslt1.1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY cli.py ./

# Exécution sans privilèges ; /data appartient à cet utilisateur.
RUN useradd --system --create-home --uid 10001 radar \
    && mkdir -p /data && chown -R radar:radar /app /data
USER radar

ENV CR_DATA_DIR=/data
EXPOSE 8010

CMD ["python", "-m", "uvicorn", "app.web.server:app", "--host", "0.0.0.0", "--port", "8010", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
