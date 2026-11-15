"""
Module Telegram pour l'envoi de notifications (sortantes uniquement).
Utilise l'API Bot Telegram pour envoyer des messages formatés et des photos.
"""

import requests
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Envoi de notifications Telegram via un bot."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.enabled = bool(bot_token and chat_id)

    def _post(self, method: str, data: dict = None, files: dict = None, timeout: int = 30) -> bool:
        """Envoi une requête POST vers l'API Telegram."""
        if not self.enabled:
            return False
        try:
            url = f"{self.base_url}/{method}"
            resp = requests.post(url, data=data, files=files, timeout=timeout)
            if resp.status_code == 200:
                return True
            logger.error(f"Telegram API erreur {resp.status_code}: {resp.text[:500]}")
            return False
        except Exception as e:
            logger.error(f"Erreur envoi Telegram: {e}")
            return False

    def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Envoie un message texte au chat configuré."""
        return self._post("sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False
        })

    def send_photo(self, photo_url: str, caption: str = "", parse_mode: str = "HTML") -> bool:
        """Envoie une photo avec caption au chat configuré."""
        if not self.enabled:
            return False
        try:
            url = f"{self.base_url}/sendPhoto"
            resp = requests.post(url, data={
                "chat_id": self.chat_id,
                "caption": caption,
                "parse_mode": parse_mode
            }, files={"photo": ("image.jpg", requests.get(photo_url, timeout=30).content, "image/jpeg")},
                timeout=60)
            if resp.status_code == 200:
                return True
            logger.error(f"Telegram photo API erreur {resp.status_code}: {resp.text[:500]}")
            return False
        except Exception as e:
            logger.error(f"Erreur envoi photo Telegram: {e}")
            return False

    def send_alert_match(self, title: str, price: float, price_drop_pct: float,
                         ai_reason: str, url: str, image_url: str = None,
                         avg_price: float = None):
        """
        Envoie une alerte 'Zéro Tri' pour une annonce retenue par le sniper.
        Contient : photo, prix, écart %, justification IA, lien direct.
        """
        # Calcul de l'écart en % par rapport au prix moyen de la Phase 1
        if avg_price and avg_price > 0:
            diff_pct = ((price - avg_price) / avg_price) * 100
            diff_text = f"<b>Écart vs moyenne Phase 1 :</b> {diff_pct:+.1f}%\n"
        else:
            diff_text = ""

        if price_drop_pct is not None and price_drop_pct > 0:
            drop_text = f"🔻 <b>Baisse de prix de {price_drop_pct:.1f}%</b>\n"
        else:
            drop_text = "✅ <b>Nouvelle annonce correspondante</b>\n"

        caption = (
            f"🎯 <b>SNIPER VINTED - ALERTE</b>\n"
            f"{drop_text}\n"
            f"📦 <b>{title}</b>\n"
            f"💰 <b>Prix :</b> {price:.2f} €\n"
            f"{diff_text}"
            f"🤖 <b>Justification IA :</b> {ai_reason}\n"
            f"🔗 <a href=\"{url}\">Voir l'annonce sur Vinted</a>"
        )

        if image_url:
            if self.send_photo(image_url, caption):
                return True
            # Fallback sur message texte si la photo échoue
        return self.send_message(caption)

    def send_tech_alert(self, message: str):
        """Envoie une alerte technique (ex: circuit-breaker déclenché)."""
        text = f"⚠️ <b>ALERTE TECHNIQUE - Vinted Sniper</b>\n\n{message}"
        return self.send_message(text)

    def send_phase1_report(self, summary: str):
        """Envoie le compte-rendu résumé de la Phase 1."""
        text = f"📊 <b>RAPPORT PHASE 1 - OBSERVATOIRE VINTED</b>\n\n{summary}"
        return self.send_message(text)