ARG BUILD_ARCH
ARG BUILD_FROM=ghcr.io/home-assistant/${BUILD_ARCH}-base:3.21
FROM ${BUILD_FROM}

# Installer les dépendances Python et outils supplémentaires
RUN apk add --no-cache python3 py3-pip py3-requests curl git build-base wget

# Créer le répertoire d'application
WORKDIR /app

# Copier le code source
COPY config.json /app/config.json
COPY run.sh /run.sh
COPY app.py /app/app.py
COPY scraper.py /app/scraper.py
COPY gemini.py /app/gemini.py
COPY telegram.py /app/telegram.py
COPY db.py /app/db.py
COPY workers.py /app/workers.py
COPY www/ /app/www/

# Installer les dépendances Python (Flask pour l'API web, Pillow pour les images Gemini Vision)
RUN pip install --no-cache-dir flask requests google-generativeai Pillow python-dateutil

# Préparer les données de l'application
RUN mkdir -p /data

# Faire du script de démarrage exécutable
RUN chmod a+x /run.sh && sed -i 's/\\r$//' /run.sh

# Exposer le port pour l'Ingress HA et le port réseau optionnel
EXPOSE 8123

# Commande par défaut : démarrer l'application
CMD ["/run.sh"]