"""
Point d'entrée principal de l'add-on Vinted Market Intelligence & Sniper.

Serveur web Flask exposant :
  * L'interface web (Ingress Home Assistant) : Dashboard, Création/Édition de recherche,
    Statistiques & Compte-rendu, Configuration Générale.
  * Une API JSON pour le frontend.
  * Les workers en arrière-plan (Phase 1 Observatoire + Phase 2 Sniper).
"""

import os
import sys
import json
import logging
import threading
from datetime import datetime

# Ajout du répertoire de l'application au sys.path
APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import db
import gemini as gemini_module
import telegram as telegram_module
import scraper as scraper_module
import workers as workers_module

from flask import Flask, jsonify, request, send_from_directory

# Permet de tester en local via variable d'environnement
DATA_DIR = os.environ.get("HAOS_DATA_DIR", "/data")
CONFIG_FILE = os.path.join(DATA_DIR, "addon_config.json")
LOG_FILE = os.path.join(DATA_DIR, "addon.log")

# Logger avec fichier rotatif (5 Mo x 3) pour l'onglet Logs du frontend
os.makedirs(DATA_DIR, exist_ok=True)
from logging.handlers import RotatingFileHandler
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(), _file_handler]
)
logger = logging.getLogger(__name__)

# Flask SANS static_url_path pour éviter le conflit avec la route /<path:filename>
app = Flask(__name__, static_folder=os.path.join(APP_DIR, "www"))


# ================================================================
# CONFIGURATION
# ================================================================

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "gemini_api_key": "",
    "gemini_models": ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"],
    "phase1_duration_days": 7,
    "scan_interval_seconds": 300,
    "max_price": 0,
    "blacklist": [],
    "ai_cleanup_interval_hours": 12,
}


class ConfigProvider:
    """Fournit la configuration courante (depuis /data/addon_config.json)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._config = dict(DEFAULT_CONFIG)
        self._load()

    def _load(self):
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._config.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            logger.error(f"Erreur chargement config: {e}")

    def get_config(self) -> dict:
        with self._lock:
            return dict(self._config)

    def save_config(self, new_config: dict) -> bool:
        with self._lock:
            try:
                self._config.update(new_config)
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                    json.dump(self._config, f, indent=2, ensure_ascii=False)
                return True
            except Exception as e:
                logger.error(f"Erreur sauvegarde config: {e}")
                return False


config_provider = ConfigProvider()

# Workers globaux
workers = None
workers_lock = threading.Lock()


def get_workers() -> workers_module.Workers:
    """Retourne l'instance globale des workers (initialisée au premier appel)."""
    global workers
    with workers_lock:
        if workers is None:
            workers = workers_module.Workers(config_provider)
            workers.start()
        return workers


# ================================================================
# UTILITAIRES API
# ================================================================

def _require_search(search_id: int):
    """Récupère une recherche ou retourne (None, erreur)."""
    search = db.get_search(search_id)
    if not search:
        return None, jsonify({"error": "Recherche introuvable"}), 404
    return search, None, None


def _validate_search_payload(data: dict):
    """Valide les champs d'une recherche. Retourne (errors, cleaned)."""
    errors = []
    name = (data.get("name") or "").strip()
    product_description = (data.get("product_description") or "").strip()
    strict_requirements = (data.get("strict_requirements") or "").strip()
    keywords = data.get("keywords") or []
    blacklist = data.get("blacklist") or []
    phase1_duration_days = data.get("phase1_duration_days", 7)
    max_price_phase2 = data.get("max_price_phase2")

    if not name:
        errors.append("Le nom de la recherche est requis.")
    if not product_description:
        errors.append("La description du produit cible est requise.")
    if not strict_requirements:
        errors.append("Les exigences strictes sont requises.")
    if not isinstance(keywords, list) or len(keywords) == 0:
        errors.append("Au moins un mot-clé est requis.")
    if not isinstance(blacklist, list):
        errors.append("La blacklist doit être une liste.")
    try:
        phase1_duration_days = int(phase1_duration_days)
        if phase1_duration_days < 1:
            errors.append("La durée d'observation doit être >= 1 jour.")
    except (ValueError, TypeError):
        errors.append("La durée d'observation doit être un nombre entier.")

    if max_price_phase2 not in (None, ""):
        try:
            max_price_phase2 = float(max_price_phase2)
        except (ValueError, TypeError):
            errors.append("Le prix plafond doit être un nombre.")
            max_price_phase2 = None

    cleaned = {
        "name": name,
        "product_description": product_description,
        "strict_requirements": strict_requirements,
        "keywords": [str(k).strip() for k in keywords if str(k).strip()],
        "blacklist": [str(b).strip() for b in blacklist if str(b).strip()],
        "phase1_duration_days": phase1_duration_days,
        "max_price_phase2": max_price_phase2,
    }
    return errors, cleaned


# ================================================================
# ROUTES FRONTEND (Ingress Home Assistant)
# ================================================================

@app.route("/")
def index():
    """Page d'accueil : Dashboard."""
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    """Fichiers statiques du frontend."""
    return send_from_directory(app.static_folder, filename)


# ================================================================
# API - RECHERCHES
# ================================================================

@app.route("/api/searches", methods=["GET"])
def api_searches():
    """Liste toutes les recherches avec leurs statuts."""
    searches = db.get_all_searches()
    return jsonify({"searches": searches})


@app.route("/api/searches", methods=["POST"])
def api_create_search():
    """Crée une nouvelle recherche."""
    data = request.get_json(force=True, silent=True) or {}
    errors, cleaned = _validate_search_payload(data)
    if errors:
        return jsonify({"errors": errors}), 400
    search_id = db.create_search(
        name=cleaned["name"],
        product_description=cleaned["product_description"],
        strict_requirements=cleaned["strict_requirements"],
        keywords=cleaned["keywords"],
        blacklist=cleaned["blacklist"],
        phase1_duration_days=cleaned["phase1_duration_days"],
        max_price_phase2=cleaned["max_price_phase2"],
    )
    return jsonify({"id": search_id, "message": "Recherche créée"}), 201


@app.route("/api/searches/<int:search_id>", methods=["GET"])
def api_get_search(search_id):
    """Détail d'une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    return jsonify({"search": search})


@app.route("/api/searches/<int:search_id>", methods=["PUT"])
def api_update_search(search_id):
    """Met à jour une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    data = request.get_json(force=True, silent=True) or {}
    errors, cleaned = _validate_search_payload(data)
    if errors:
        return jsonify({"errors": errors}), 400
    db.update_search(search_id, **cleaned)
    return jsonify({"message": "Recherche mise à jour"})


@app.route("/api/searches/<int:search_id>", methods=["DELETE"])
def api_delete_search(search_id):
    """Supprime une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    # Suppression des annonces liées
    with db.get_connection(db.OBSERVATORY_DB) as conn:
        conn.execute("DELETE FROM listings WHERE search_id = ?", (search_id,))
        conn.execute("DELETE FROM scan_logs WHERE search_id = ?", (search_id,))
        conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    with db.get_connection(db.SNIPER_AUDIT_DB) as conn:
        conn.execute("DELETE FROM sniper_audit WHERE search_id = ?", (search_id,))
        conn.execute("DELETE FROM alerts WHERE search_id = ?", (search_id,))
    return jsonify({"message": "Recherche supprimée"})


# ================================================================
# API - CONTRÔLE DES PHASES
# ================================================================

@app.route("/api/searches/<int:search_id>/start_phase1", methods=["POST"])
def api_start_phase1(search_id):
    """Démarre la Phase 1 (Observatoire) pour une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    # Statuts acceptés : draft, phase1_done (après clôture), paused (en attente de reprise)
    if search.get("status") not in ("draft", "phase1_done", "paused"):
        return jsonify({"error": "Statut incompatible pour démarrer la Phase 1"}), 400
    db.update_search(search_id, status="phase1_running", phase1_started_at=datetime.utcnow().isoformat())
    return jsonify({"message": "Phase 1 démarrée", "status": "phase1_running"})


@app.route("/api/searches/<int:search_id>/start_phase2", methods=["POST"])
def api_start_phase2(search_id):
    """Active manuellement la Phase 2 (Sniper) pour une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    # Phase 2 uniquement possible après clôture de la Phase 1 (statut 'phase1_done' / 'En attente')
    if search.get("status") != "phase1_done":
        return jsonify({"error": "La Phase 1 doit être terminée (statut 'En attente')"}), 400

    # Récupération sécurisée du prix plafond optionnel (corps JSON absent = pas de prix plafond)
    json_payload = request.get_json(force=True, silent=True)
    if json_payload is None:
        json_payload = {}
    max_price_phase2 = json_payload.get("max_price_phase2")
    if max_price_phase2 is not None:
        try:
            max_price_phase2 = float(max_price_phase2)
            db.update_search(search_id, max_price_phase2=max_price_phase2)
        except (ValueError, TypeError):
            return jsonify({"error": "Le prix plafond doit être un nombre."}), 400

    db.update_search(search_id, status="phase2_running", phase2_started_at=datetime.utcnow().isoformat())
    return jsonify({"message": "Phase 2 activée", "status": "phase2_running"})


@app.route("/api/searches/<int:search_id>/pause", methods=["POST"])
def api_pause_search(search_id):
    """Met une recherche en pause."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    db.update_search(search_id, status="paused")
    return jsonify({"message": "Recherche mise en pause", "status": "paused"})


@app.route("/api/searches/<int:search_id>/resume", methods=["POST"])
def api_resume_search(search_id):
    """Reprend une recherche en pause (retourne à la phase précédente)."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    # Reprend selon la dernière phase connue
    if search.get("phase2_started_at"):
        new_status = "phase2_running"
    elif search.get("phase1_started_at"):
        new_status = "phase1_running"
    else:
        new_status = "draft"
    db.update_search(search_id, status=new_status)
    return jsonify({"message": "Recherche reprise", "status": new_status})


# ================================================================
# API - STATISTIQUES & COMPTE-RENDU
# ================================================================

@app.route("/api/searches/<int:search_id>/stats", methods=["GET"])
def api_search_stats(search_id):
    """Statistiques & compte-rendu d'une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code

    listing_stats = db.get_listing_stats(search_id)
    sniper_stats = db.get_sniper_stats(search_id)
    listings = db.get_all_listings_for_search(search_id)

    # Détails des annonces supprimées (deleted)
    deleted = [l for l in listings if l.get("status") == "deleted"]
    total = listing_stats.get("total", 0)
    deleted_pct = (len(deleted) / total * 100) if total > 0 else 0

    # Durées de vente
    durations = []
    for l in listings:
        if l.get("first_seen_at") and l.get("ended_at"):
            try:
                d1 = datetime.fromisoformat(l["first_seen_at"])
                d2 = datetime.fromisoformat(l["ended_at"])
                durations.append((d2 - d1).total_seconds() / 3600)
            except Exception:
                pass
    avg_sale_time = sum(durations) / len(durations) if durations else None

    # Construction des métriques clés pour le frontend
    price_stats = listing_stats.get("price_stats", {}) or {}

    # Calcul du prix médian sur les annonces validées + sold
    sold_prices = [l.get("final_price") for l in listings
                   if l.get("ai_status") == "validated" and l.get("status") == "sold" and l.get("final_price")]
    sold_prices.sort()
    median_price = 0.0
    if sold_prices:
        n = len(sold_prices)
        median_price = sold_prices[n // 2] if n % 2 else (sold_prices[n // 2 - 1] + sold_prices[n // 2]) / 2

    metrics = {
        "total": total,
        "total_sold": price_stats.get("count", 0) or 0,
        "avg_price": price_stats.get("avg_price", 0) or 0,
        "median_price": median_price,
        "avg_days_to_sell": (avg_sale_time / 24) if avg_sale_time else 0,
        "deleted_pct": round(deleted_pct, 2),
        "total_deleted": len(deleted),
        "total_validated": listing_stats.get("by_ai_status", {}).get("validated", 0) or 0,
    }

    return jsonify({
        "search": search,
        "listing_stats": listing_stats,
        "sniper_stats": sniper_stats,
        "ai_report": search.get("ai_report"),
        "deleted": {"count": len(deleted), "pct": round(deleted_pct, 2), "items": deleted[:50]},
        "avg_sale_time_hours": avg_sale_time,
        "metrics": metrics,
        "listings": listings,
    })


@app.route("/api/searches/<int:search_id>/listings", methods=["GET"])
def api_search_listings(search_id):
    """Liste paginée des annonces d'une recherche."""
    search, err, code = _require_search(search_id)
    if err:
        return err, code
    status_filter = request.args.get("status")
    ai_filter = request.args.get("ai_status")
    limit = request.args.get("limit", default=100, type=int)
    listings = db.get_all_listings_for_search(search_id)
    if status_filter:
        listings = [l for l in listings if l.get("status") == status_filter]
    if ai_filter:
        listings = [l for l in listings if l.get("ai_status") == ai_filter]
    return jsonify({"listings": listings[:limit], "total": len(listings)})


# ================================================================
# API - ASSISTANT IA (MOTS-CLÉS)
# ================================================================

@app.route("/api/ai/generate-keywords", methods=["POST"])
def api_generate_keywords():
    """
    Assistant IA de mots-clés : envoie le produit cible à Gemini pour générer
    simultanément une liste de mots-clés élargis et une liste initiale de blacklist.
    """
    data = request.get_json(force=True, silent=True) or {}
    product_description = (data.get("product_description") or "").strip()
    strict_requirements = (data.get("strict_requirements") or "").strip()
    if not product_description:
        return jsonify({"error": "La description du produit est requise."}), 400

    cfg = config_provider.get_config()
    gemini = gemini_module.GeminiClient(cfg.get("gemini_api_key", ""), cfg.get("gemini_models", []))
    if not gemini.is_available():
        return jsonify({"error": "Clé API Gemini manquante ou aucun modèle configuré."}), 400

    result = gemini.generate_keywords(product_description, strict_requirements)
    if not result:
        return jsonify({"error": "Échec de génération des mots-clés par l'IA (quota ou indisponibilité)."}), 502
    return jsonify({"keywords": result.get("keywords", []), "blacklist": result.get("blacklist", [])})


# ================================================================
# API - CONFIGURATION GÉNÉRALE
# ================================================================

@app.route("/api/config", methods=["GET"])
def api_get_config():
    """Configuration générale (sans secrets sensibles exposés en clair)."""
    cfg = config_provider.get_config()
    safe = dict(cfg)
    safe["telegram_bot_token"] = "***" if cfg.get("telegram_bot_token") else ""
    safe["gemini_api_key"] = "***" if cfg.get("gemini_api_key") else ""
    return jsonify({"config": safe})


@app.route("/api/config", methods=["POST"])
def api_save_config():
    """Sauvegarde la configuration générale."""
    data = request.get_json(force=True, silent=True) or {}
    allowed = ["telegram_bot_token", "telegram_chat_id", "gemini_api_key",
               "gemini_models", "scan_interval_seconds", "max_price", "blacklist",
               "ai_cleanup_interval_hours", "phase1_duration_days"]
    new_config = {}
    for key in allowed:
        if key in data:
            new_config[key] = data[key]
    if "gemini_models" in new_config and not isinstance(new_config["gemini_models"], list):
        return jsonify({"error": "gemini_models doit être une liste ordonnée."}), 400
    # Ne pas écraser la clé Gemini si elle est vide ou masquée (sécurité)
    if new_config.get("gemini_api_key") in ("", "***", None):
        current_cfg = config_provider.get_config()
        new_config["gemini_api_key"] = current_cfg.get("gemini_api_key", "")
    config_provider.save_config(new_config)
    # Redémarre les workers pour appliquer la nouvelle config
    try:
        w = get_workers()
        w.restart()
    except Exception as e:
        logger.error(f"Erreur redémarrage workers: {e}")
    return jsonify({"message": "Configuration sauvegardée"})


# ================================================================
# API - ÉTAT DU SYSTÈME (DASHBOARD)
# ================================================================

@app.route("/api/system/status", methods=["GET"])
def api_system_status():
    """État global du système pour le dashboard."""
    cfg = config_provider.get_config()
    searches = db.get_all_searches()
    scraper_info = {"blocked": False, "requests_count": 0}
    if workers and workers.scraper:
        scraper_info = {
            "blocked": workers.scraper.is_blocked(),
            "requests_count": workers.scraper.requests_count,
        }
    return jsonify({
        "system": {
            "gemini_enabled": bool(cfg.get("gemini_api_key")),
            "gemini_models": cfg.get("gemini_models", []),
            "telegram_enabled": bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")),
            "scraper": scraper_info,
            "uptime": datetime.utcnow().isoformat(),
        },
        "searches": searches,
    })


# ================================================================
# DÉMARRAGE
# ================================================================

def main():
    """Point d'entrée : initialise les bases de données et démarre le serveur."""
    port = int(os.environ.get("PORT", 8124))
    host = os.environ.get("HOST", "0.0.0.0")

    # Initialisation des bases de données
    db.init_all_databases()
    logger.info("Bases de données initialisées")

    # Démarrage des workers en arrière-plan
    get_workers()
    logger.info("Application Vinted Market Intelligence & Sniper démarrée")

    app.run(host=host, port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()