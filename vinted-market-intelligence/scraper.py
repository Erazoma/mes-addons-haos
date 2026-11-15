"""
Module de scraping Vinted avec protection anti-ban.

Vinted bloquant les comportements robotiques, le module intègre :
* Initialisation et renouvellement propre des cookies/headers de session.
* Comportement humain : intervalles de scan aléatoires (jitter) et pauses régulières.
* Disjoncteur automatique (Circuit-Breaker) : si le scraper rencontre 3 erreurs
  consécutives ou un blocage (HTTP 403, 429, Captcha), il se met immédiatement
  en pause pendant plusieurs heures et envoie une alerte technique sur Telegram.
"""

import requests
import random
import time
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


class CircuitBreakerOpen(Exception):
    """Exception levée quand le circuit-breaker est ouvert (scraper en pause)."""
    pass


class VintedScraper:
    """Scraper Vinted avec comportement humain et circuit-breaker."""

    BASE_URL = "https://www.vinted.fr"
    API_URL = f"{BASE_URL}/api/v2"

    # Headers réalistes pour simuler un navigateur
    DEFAULT_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Referer": "https://www.vinted.fr/",
        "Origin": "https://www.vinted.fr",
    }

    def __init__(self, telegram_notifier=None):
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
        self.telegram = telegram_notifier
        # Circuit-breaker
        self.error_count = 0
        self.max_errors = 3
        self.blocked_until = None  # datetime jusqu'auquel le scraper est en pause
        self.block_duration_hours = 4  # pause de plusieurs heures
        # Stats de la session
        self.requests_count = 0

    # ================================================================
    # GESTION DES COOKIES / SESSION
    # ================================================================

    def init_session(self) -> bool:
        """Initialise proprement la session : cookies, headers, tokens CSRF."""
        try:
            resp = self.session.get(self.BASE_URL, timeout=30)
            self.requests_count += 1
            if resp.status_code == 200:
                logger.info("Session Vinted initialisée avec succès")
                return True
            logger.warning(f"Init session Vinted: HTTP {resp.status_code}")
        except Exception as e:
            logger.error(f"Erreur init session Vinted: {e}")
        return False

    def rotate_session(self):
        """Renouvelle les cookies/headers pour simuler un renouvellement de session."""
        self.session.close()
        self.session = requests.Session()
        self.session.headers.update(self.DEFAULT_HEADERS)
        self.init_session()

    # ================================================================
    # CIRCUIT-BREAKER
    # ================================================================

    def is_blocked(self) -> bool:
        """Vérifie si le scraper est actuellement en pause (circuit ouvert)."""
        if self.blocked_until is None:
            return False
        if datetime.utcnow() >= self.blocked_until:
            self.blocked_until = None
            self.error_count = 0
            return False
        return True

    def record_error(self, error_type: str = "generic"):
        """Enregistre une erreur. Après 3 erreurs consécutives ou un blocage, ouvre le circuit."""
        self.error_count += 1
        logger.warning(f"Erreur scraper #{self.error_count} ({error_type})")
        if self.error_count >= self.max_errors or error_type in ("blocked", "captcha"):
            self.open_circuit()

    def open_circuit(self):
        """Ouvre le circuit-breaker : pause de plusieurs heures + alerte Telegram."""
        self.blocked_until = datetime.utcnow() + timedelta(hours=self.block_duration_hours)
        self.error_count = 0
        msg = (
            f"Le scraper Vinted a détecté un blocage (3 erreurs consécutives, "
            f"HTTP 403/429 ou Captcha).<br>"
            f"Il est mis en pause automatique pendant {self.block_duration_hours} heures.<br>"
            f"Heure UTC : {datetime.utcnow().isoformat()}"
        )
        logger.error(f"Circuit-breaker ouvert : {msg}")
        if self.telegram:
            self.telegram.send_tech_alert(msg)

    # ================================================================
    # REQUÊTES AVEC COMPORTEMENT HUMAIN
    # ================================================================

    def _human_delay(self, base_delay: float = 1.5):
        """Pause aléatoire (jitter) entre les requêtes pour simuler un humain."""
        jitter = random.uniform(0.3, base_delay + 1.0)
        time.sleep(jitter)

    def _request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Requête HTTP protégée : gestion des erreurs, circuit-breaker, jitter."""
        if self.is_blocked():
            raise CircuitBreakerOpen(
                f"Scraper en pause jusqu'à {self.blocked_until.isoformat()}"
            )

        self._human_delay()

        try:
            resp = self.session.request(method, url, timeout=30, **kwargs)
            self.requests_count += 1

            if resp.status_code == 403:
                logger.error("HTTP 403 - Blocage détecté")
                self.record_error("blocked")
                return None
            elif resp.status_code == 429:
                logger.error("HTTP 429 - Trop de requêtes")
                self.record_error("blocked")
                time.sleep(random.uniform(30, 90))
                return None
            elif resp.status_code >= 500:
                logger.error(f"HTTP {resp.status_code} - Erreur serveur")
                self.record_error("server_error")
                return None
            elif resp.status_code != 200:
                logger.warning(f"HTTP {resp.status_code} inattendu")
                self.record_error("http_error")
                return None

            # Succès : on réarme le compteur d'erreurs
            self.error_count = 0
            return resp

        except requests.exceptions.RequestException as e:
            logger.error(f"Erreur réseau: {e}")
            self.record_error("network_error")
            return None

    # ================================================================
    # SCRAPING DES ANNONCES
    # ================================================================

    def search_listings(self, keyword: str, max_pages: int = 3) -> List[Dict[str, Any]]:
        """Scraping des annonces pour un mot-clé donné via l'API Vinted."""
        listings = []
        try:
            for page in range(1, max_pages + 1):
                if self.is_blocked():
                    raise CircuitBreakerOpen("Circuit ouvert pendant le scan")

                params = {
                    "search_text": keyword,
                    "page": page,
                    "per_page": 20,
                }
                resp = self._request("GET", f"{self.API_URL}/catalog/search", params=params)
                if resp is None:
                    continue

                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    logger.warning(f"Réponse non-JSON pour '{keyword}' page {page}")
                    continue

                items = data.get("items", [])
                if not items:
                    break

                for item in items:
                    listing = self._normalize_item(item, keyword)
                    if listing:
                        listings.append(listing)

                # Pause entre les pages (comportement humain)
                self._human_delay(base_delay=2.5)

        except CircuitBreakerOpen:
            raise
        except Exception as e:
            logger.error(f"Erreur scraping '{keyword}': {e}")
            self.record_error("exception")

        return listings

    def _normalize_item(self, item: Dict[str, Any], keyword: str) -> Optional[Dict[str, Any]]:
        """Normalise une annonce brute de l'API Vinted en dict standardisé."""
        try:
            vinted_id = item.get("id")
            if not vinted_id:
                return None
            title = item.get("title", "")
            price = item.get("price", {})
            amount = price.get("amount") if isinstance(price, dict) else price
            if amount is None:
                amount = 0.0
            status = "active"
            if item.get("status") == "sold" or item.get("sold"):
                status = "sold"
            elif item.get("status") == "deleted" or item.get("deleted"):
                status = "deleted"

            photos = item.get("photos", [])
            image_url = None
            if photos and isinstance(photos, list):
                first = photos[0] if isinstance(photos[0], dict) else {}
                image_url = first.get("url") or first.get("url_large") or first.get("thumbnail")

            url = f"https://www.vinted.fr/items/{vinted_id}"

            return {
                "vinted_id": vinted_id,
                "keyword_used": keyword,
                "title": title,
                "description": item.get("description", ""),
                "category": item.get("category", {}).get("name", "") if isinstance(item.get("category"), dict) else "",
                "brand": item.get("brand", {}).get("title", "") if isinstance(item.get("brand"), dict) else "",
                "condition": item.get("condition", {}).get("title", "") if isinstance(item.get("condition"), dict) else "",
                "initial_price": float(amount),
                "final_price": float(amount) if status == "sold" else None,
                "status": status,
                "created_at_vinted": item.get("created_at"),
                "ended_at": item.get("ended_at"),
                "url": url,
                "image_url": image_url,
                "local_image_path": None,
            }
        except Exception as e:
            logger.error(f"Erreur normalisation annonce: {e}")
            return None

    def download_image(self, image_url: str) -> Optional[bytes]:
        """Télécharge une image pour analyse Vision."""
        if not image_url:
            return None
        try:
            resp = self._request("GET", image_url)
            if resp is not None:
                return resp.content
        except Exception as e:
            logger.error(f"Erreur téléchargement image {image_url}: {e}")
        return None

    def health_check(self) -> bool:
        """Vérification rapide de la connectivité Vinted."""
        try:
            resp = self.session.get(self.BASE_URL, timeout=15)
            return resp.status_code == 200
        except Exception:
            return False