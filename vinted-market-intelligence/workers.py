"""
Workers en arrière-plan pour l'add-on Vinted Market Intelligence.

Phase 1 - L'Observatoire : scan continu, filtrage Blacklist, nettoyage IA par lots,
clôture avec rapport IA + Telegram.
Phase 2 - Le Sniper : détection haute fréquence, filtrage séquentiel, notifications
Telegram "Zéro Tri".
"""

import threading
import time
import random
import logging
from datetime import datetime, timedelta

import db
import gemini as gemini_module
import telegram as telegram_module
import scraper as scraper_module

logger = logging.getLogger(__name__)


class Workers:
    """Gestionnaire des workers en arrière-plan."""

    def __init__(self, config_provider):
        self.config_provider = config_provider
        self.scraper = None
        self.gemini = None
        self.telegram = None
        self._stop_event = threading.Event()
        self._threads = []

    # ================================================================
    # INITIALISATION DES COMPOSANTS
    # ================================================================

    def _init_components(self):
        """(Re)crée les composants scraper/gemini/telegram depuis la config."""
        cfg = self.config_provider.get_config()
        self.telegram = telegram_module.TelegramNotifier(
            cfg.get("telegram_bot_token", ""),
            cfg.get("telegram_chat_id", "")
        )
        self.gemini = gemini_module.GeminiClient(
            cfg.get("gemini_api_key", ""),
            cfg.get("gemini_models", [])
        )
        self.scraper = scraper_module.VintedScraper(telegram_notifier=self.telegram)

    # ================================================================
    # DÉMARRAGE / ARRÊT
    # ================================================================

    def start(self):
        """Démarre tous les workers en arrière-plan."""
        self._init_components()
        self._stop_event.clear()
        self._threads = []

        threads = [
            threading.Thread(target=self._phase1_worker_loop, name="Phase1Worker", daemon=True),
            threading.Thread(target=self._phase2_worker_loop, name="Phase2Worker", daemon=True),
            threading.Thread(target=self._ai_cleanup_loop, name="AiCleanup", daemon=True),
            threading.Thread(target=self._alert_sender_loop, name="AlertSender", daemon=True),
        ]
        for t in threads:
            t.start()
            self._threads.append(t)
        logger.info("Workers démarrés")

    def stop(self):
        """Arrête tous les workers."""
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=5)
        logger.info("Workers arrêtés")

    def restart(self):
        """Redémarre les workers."""
        self.stop()
        self._init_components()
        self.start()

    # ================================================================
    # UTILITAIRES
    # ================================================================

    def _blacklist_hit(self, title: str, description: str, blacklist: list) -> bool:
        """Vérifie si le titre ou la description contient un mot de la Blacklist."""
        text = f"{title or ''} {description or ''}".lower()
        for word in blacklist:
            if word and word.lower() in text:
                return True
        return False

    def _jitter_interval(self, base_seconds: float) -> float:
        """Intervalles de scan aléatoires (jitter anti-ban)."""
        return base_seconds * random.uniform(0.7, 1.4)

    # ================================================================
    # WORKER PHASE 1 - L'Observatoire
    # ================================================================

    def _phase1_worker_loop(self):
        """Boucle de scan Phase 1 : collecte massive des annonces."""
        while not self._stop_event.is_set():
            try:
                searches = db.get_all_searches()
                for search in searches:
                    if search.get("status") != "phase1_running":
                        continue
                    self._run_phase1_scan(search)
                    time.sleep(self._jitter_interval(5))

                self._check_phase1_closure()

                interval = self.config_provider.get_config().get("scan_interval_seconds", 300)
                time.sleep(self._jitter_interval(interval))
            except Exception as e:
                logger.error(f"Erreur worker Phase 1: {e}")
                time.sleep(30)

    def _run_phase1_scan(self, search: dict):
        """Effectue un scan complet pour une recherche en Phase 1."""
        search_id = search["id"]
        keywords = search.get("keywords", [])
        blacklist = search.get("blacklist", [])

        for keyword in keywords:
            if self._stop_event.is_set():
                return
            try:
                if self.scraper.is_blocked():
                    logger.warning("Scraper bloqué (circuit-breaker), scan Phase 1 suspendu")
                    return

                listings = self.scraper.search_listings(keyword, max_pages=3)
                new_count = 0
                updated_count = 0
                for raw in listings:
                    title = raw.get("title", "")
                    description = raw.get("description", "")
                    # Filtre Blacklist avant enregistrement
                    if self._blacklist_hit(title, description, blacklist):
                        continue
                    raw["search_id"] = search_id
                    is_new = db.upsert_listing(raw)
                    if is_new:
                        new_count += 1
                    else:
                        updated_count += 1

                db.log_scan(search_id, keyword, len(listings), new_count, updated_count)
                logger.info(f"Phase 1 scan '{keyword}': {len(listings)} trouvées, "
                            f"{new_count} nouvelles, {updated_count} mises à jour")

            except scraper_module.CircuitBreakerOpen:
                logger.warning("Circuit-breaker ouvert, arrêt du scan Phase 1")
                return
            except Exception as e:
                logger.error(f"Erreur scan Phase 1 '{keyword}': {e}")
                db.log_scan(search_id, keyword, 0, 0, 0, error=str(e))

    def _check_phase1_closure(self):
        """Vérifie si des recherches Phase 1 ont atteint leur durée configurée."""
        searches = db.get_all_searches()
        for search in searches:
            if search.get("status") != "phase1_running":
                continue
            started = search.get("phase1_started_at")
            if not started:
                continue
            try:
                started_dt = datetime.fromisoformat(started)
                duration_days = search.get("phase1_duration_days", 7)
                end_dt = started_dt + timedelta(days=duration_days)
                if datetime.utcnow() >= end_dt:
                    self._close_phase1(search["id"])
            except Exception as e:
                logger.error(f"Erreur clôture Phase 1 search {search['id']}: {e}")

    def _close_phase1(self, search_id: int):
        """Clôture la Phase 1 : rapport IA, Telegram, passage en 'En attente'."""
        logger.info(f"Clôture Phase 1 pour recherche {search_id}")
        try:
            search = db.get_search(search_id)
            if not search:
                return

            listings = db.get_validated_sold_listings(search_id)
            stats = self._compute_stats(listings, search_id)

            report_text = None
            if self.gemini and self.gemini.is_available():
                report_text = self.gemini.generate_market_report(
                    stats, search["product_description"])

            report = {
                "generated_at": datetime.utcnow().isoformat(),
                "stats": stats,
                "report_text": report_text,
                "listings_count": stats.get("sold_validated_count", 0),
            }
            db.update_search(search_id, status="phase1_done", ai_report=report)

            if self.telegram and report_text:
                self.telegram.send_phase1_report(report_text[:2000])

            logger.info(f"Phase 1 clôturée pour recherche {search_id}")
        except Exception as e:
            logger.error(f"Erreur clôture Phase 1 {search_id}: {e}")

    def _compute_stats(self, listings: list, search_id: int) -> dict:
        """Calcule les métriques clés sur les annonces validated + sold."""
        prices = [l["final_price"] for l in listings if l.get("final_price")]
        sold_validated_count = len(listings)
        avg_price = sum(prices) / len(prices) if prices else None
        min_price = min(prices) if prices else None
        max_price = max(prices) if prices else None

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

        all_listings = db.get_all_listings_for_search(search_id)
        total = len(all_listings)
        deleted_count = sum(1 for l in all_listings if l.get("status") == "deleted")
        deleted_pct = (deleted_count / total * 100) if total > 0 else 0

        return {
            "total": total,
            "sold_validated_count": sold_validated_count,
            "avg_price": avg_price,
            "min_price": min_price,
            "max_price": max_price,
            "avg_sale_time_hours": avg_sale_time,
            "deleted_count": deleted_count,
            "deleted_pct": round(deleted_pct, 2),
        }

    # ================================================================
    # WORKER IA - Nettoyage par lots (Filtrage en entonnoir)
    # ================================================================

    def _ai_cleanup_loop(self):
        """
        Worker tournant périodiquement (ex: toutes les 12h) sur les annonces
        au statut 'pending' pour lisser la consommation des quotas Gemini.
        """
        while not self._stop_event.is_set():
            try:
                searches = db.get_all_searches()
                for search in searches:
                    if search.get("status") not in ("phase1_running", "phase1_done"):
                        continue
                    self._process_pending_listings(search)
            except Exception as e:
                logger.error(f"Erreur worker IA cleanup: {e}")

            # Pause longue entre les lots (12h par défaut)
            cleanup_interval = self.config_provider.get_config().get("ai_cleanup_interval_hours", 12)
            for _ in range(int(cleanup_interval * 3600)):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _process_pending_listings(self, search: dict):
        """Analyse les annonces pending via le filtrage en entonnoir (Étape A puis B)."""
        search_id = search["id"]
        requirements = search.get("strict_requirements", "")
        keywords = search.get("keywords", [])
        pending = db.get_pending_listings(search_id, limit=20)

        for listing in pending:
            if self._stop_event.is_set():
                return
            if not self.gemini or not self.gemini.is_available():
                return

            try:
                # Étape A (Filtre Textuel)
                result = self.gemini.analyze_listing_text(
                    listing.get("title", ""),
                    listing.get("description", ""),
                    requirements,
                    keywords
                )

                if result is None:
                    continue

                decision = result.get("decision")
                reason = result.get("reason", "")

                if decision == "uncertain":
                    # Étape B (Levée de doute par Vision)
                    image_bytes = self.scraper.download_image(listing.get("image_url", ""))
                    if image_bytes:
                        vision_result = self.gemini.analyze_listing_vision(
                            listing.get("title", ""),
                            listing.get("description", ""),
                            requirements,
                            image_bytes
                        )
                        if vision_result:
                            decision = vision_result.get("decision")
                            reason = vision_result.get("reason", reason)

                if decision == "validated":
                    db.update_listing_ai_status(listing["vinted_id"], "validated", reason)
                elif decision == "rejected":
                    db.update_listing_ai_status(listing["vinted_id"], "rejected", reason)

                time.sleep(random.uniform(2, 5))

            except Exception as e:
                logger.error(f"Erreur analyse IA listing {listing.get('vinted_id')}: {e}")

    # ================================================================
    # WORKER PHASE 2 - Le Sniper (Temps Réel)
    # ================================================================

    def _phase2_worker_loop(self):
        """Boucle de surveillance temps réel Phase 2."""
        while not self._stop_event.is_set():
            try:
                searches = db.get_all_searches()
                for search in searches:
                    if search.get("status") != "phase2_running":
                        continue
                    self._run_phase2_scan(search)
                    time.sleep(self._jitter_interval(3))

                interval = self.config_provider.get_config().get("scan_interval_seconds", 300)
                time.sleep(self._jitter_interval(interval))
            except Exception as e:
                logger.error(f"Erreur worker Phase 2: {e}")
                time.sleep(30)

    def _get_avg_price(self, search_id: int):
        """Récupère le prix moyen calculé en Phase 1."""
        search = db.get_search(search_id)
        if search and search.get("ai_report"):
            stats = search["ai_report"].get("stats", {})
            return stats.get("avg_price")
        return None

    def _run_phase2_scan(self, search: dict):
        """Surveille les nouvelles annonces et applique le pipeline de filtrage."""
        search_id = search["id"]
        keywords = search.get("keywords", [])
        blacklist = search.get("blacklist", [])
        max_price = search.get("max_price_phase2")
        requirements = search.get("strict_requirements", "")
        avg_price = self._get_avg_price(search_id)

        for keyword in keywords:
            if self._stop_event.is_set():
                return
            try:
                if self.scraper.is_blocked():
                    return

                listings = self.scraper.search_listings(keyword, max_pages=2)
                for raw in listings:
                    if self._stop_event.is_set():
                        return
                    title = raw.get("title", "")
                    description = raw.get("description", "")
                    price = raw.get("initial_price", 0) or 0

                    # Filtre 1 (Règles dures) : Blacklist ou prix > prix max
                    if self._blacklist_hit(title, description, blacklist):
                        continue
                    if max_price and price > max_price:
                        continue

                    # Filtre 2 (Vérification sniper_audit.db)
                    existing = db.get_sniper_audit(raw["vinted_id"])
                    if existing:
                        if existing.get("ai_decision") == "rejected":
                            continue
                        if existing.get("ai_decision") == "retained":
                            last_price = existing.get("last_seen_price", 0) or 0
                            if price < last_price:
                                drop_pct = ((last_price - price) / last_price) * 100
                                db.upsert_sniper_audit({
                                    "vinted_id": raw["vinted_id"],
                                    "search_id": search_id,
                                    "keyword_used": keyword,
                                    "title": title,
                                    "last_seen_price": price,
                                    "ai_decision": "retained",
                                    "ai_reason": existing.get("ai_reason", ""),
                                })
                                alert_id = db.create_alert(raw["vinted_id"], search_id,
                                                            "price_drop", price, drop_pct)
                                self._send_alert_if_possible(alert_id, raw, search_id, avg_price,
                                                              drop_pct=drop_pct)
                            continue

                    # Filtre 3 (Validation IA à la volée - Tolérance zéro)
                    if not self.gemini or not self.gemini.is_available():
                        continue

                    text_result = self.gemini.analyze_listing_text(
                        title, description, requirements, keywords)

                    if text_result is None:
                        continue

                    decision = text_result.get("decision")
                    reason = text_result.get("reason", "")

                    if decision == "uncertain":
                        image_bytes = self.scraper.download_image(raw.get("image_url", ""))
                        if image_bytes:
                            vision_result = self.gemini.analyze_listing_vision(
                                title, description, requirements, image_bytes)
                            if vision_result:
                                decision = vision_result.get("decision")
                                reason = vision_result.get("reason", reason)

                    if decision == "validated":
                        db.upsert_sniper_audit({
                            "vinted_id": raw["vinted_id"],
                            "search_id": search_id,
                            "keyword_used": keyword,
                            "title": title,
                            "last_seen_price": price,
                            "ai_decision": "retained",
                            "ai_reason": reason,
                        })
                        alert_id = db.create_alert(raw["vinted_id"], search_id, "new_match", price)
                        self._send_alert_if_possible(alert_id, raw, search_id, avg_price)
                    else:
                        db.upsert_sniper_audit({
                            "vinted_id": raw["vinted_id"],
                            "search_id": search_id,
                            "keyword_used": keyword,
                            "title": title,
                            "last_seen_price": price,
                            "ai_decision": "rejected",
                            "ai_reason": reason,
                        })

                    time.sleep(random.uniform(3, 6))

            except scraper_module.CircuitBreakerOpen:
                return
            except Exception as e:
                logger.error(f"Erreur scan Phase 2 '{keyword}': {e}")
                db.log_scan(search_id, keyword, 0, 0, 0, error=str(e))

    def _send_alert_if_possible(self, alert_id: int, raw: dict, search_id: int,
                                 avg_price, drop_pct=None):
        """Envoie la notification Telegram si possible, sinon laisse l'alerte en file."""
        if not self.telegram or not self.telegram.enabled:
            return
        sent = self.telegram.send_alert_match(
            title=raw.get("title", ""),
            price=raw.get("initial_price", 0) or 0,
            price_drop_pct=drop_pct,
            ai_reason=raw.get("ai_reason", ""),
            url=raw.get("url", ""),
            image_url=raw.get("image_url"),
            avg_price=avg_price
        )
        if sent:
            db.mark_alert_sent(alert_id)
            db.update_sniper_notified(raw["vinted_id"])

    # ================================================================
    # WORKER ENVOI DES ALERTES EN FILE
    # ================================================================

    def _alert_sender_loop(self):
        """Envoie les alertes restantes non envoyées (file d'attente Telegram)."""
        while not self._stop_event.is_set():
            try:
                alerts = db.get_unsent_alerts(limit=5)
                for alert in alerts:
                    if not self.telegram or not self.telegram.enabled:
                        break
                    search = db.get_search(alert.get("search_id"))
                    avg_price = self._get_avg_price(alert.get("search_id")) if search else None
                    caption = self._build_alert_caption(alert, avg_price)
                    if caption:
                        sent = self.telegram.send_message(caption)
                        if sent:
                            db.mark_alert_sent(alert["id"])
                            db.update_sniper_notified(alert["vinted_id"])
                    time.sleep(2)
            except Exception as e:
                logger.error(f"Erreur envoi alertes: {e}")
            time.sleep(60)

    def _build_alert_caption(self, alert: dict, avg_price):
        """Construit le texte d'une alerte Telegram."""
        title = alert.get("title", "Annonce Vinted")
        price = alert.get("price", 0) or 0
        url = alert.get("url", "")
        alert_type = alert.get("alert_type", "new_match")

        if avg_price and avg_price > 0:
            diff_pct = ((price - avg_price) / avg_price) * 100
            diff_text = f"\n💰 Écart vs moyenne Phase 1 : {diff_pct:+.1f}%"
        else:
            diff_text = ""

        if alert_type == "price_drop":
            drop = alert.get("price_drop_pct")
            drop_text = f"\n🔻 Baisse de prix de {drop:.1f}%" if drop else "\n🔻 Baisse de prix"
        else:
            drop_text = "\n✅ Nouvelle annonce correspondante"

        return (
            f"🎯 <b>SNIPER VINTED - ALERTE</b>\n"
            f"{drop_text}\n"
            f"📦 <b>{title}</b>\n"
            f"💰 <b>Prix :</b> {price:.2f} €"
            f"{diff_text}\n"
            f"🔗 <a href=\"{url}\">Voir l'annonce sur Vinted</a>"
        )