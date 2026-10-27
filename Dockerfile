# Mains de Pinkfloyd — worker navigateur (Playwright + Mistral Workflows)
# Déploiement géré Mistral : l'image doit démarrer le worker elle-même,
# aucune clé API n'est stockée (token service-account injecté par la plateforme).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# SDK Workflows >= 3.10 : requis pour lire le token du service-account.
# Playwright figé pour coller au Chromium installé juste après.
RUN pip install "mistralai-workflows[mistralai]>=3.10" "pydantic>=2.0" "playwright==1.52.0"

# Chromium headless + dépendances système.
RUN python -m playwright install --with-deps chromium

COPY src /app/src

CMD ["python", "/app/src/worker.py"]
