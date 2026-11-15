"""
Module Gemini AI avec fallback en cascade.

L'utilisateur configure une liste ordonnée de modèles Gemini (3-4 modèles).
Si un modèle renvoie une erreur de quota (HTTP 429) ou une indisponibilité,
le code bascule automatiquement sur le modèle suivant dans la liste.

Règle de "Tolérance Zéro sur le Doute" : l'IA doit appliquer une rigueur
absolue sur les spécifications techniques exigées. En cas d'ambiguïté
persistante (même après analyse de l'image), l'annonce doit être rejetée.
"""

import requests
import json
import logging
import time
import base64
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


class GeminiClient:
    """Client Gemini avec fallback en cascade."""

    def __init__(self, api_key: str, models: List[str]):
        self.api_key = api_key
        self.models = models or ["gemini-2.5-flash"]
        self.current_model_index = 0
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        self.enabled = bool(api_key)

    def _get_model_name(self) -> str:
        return self.models[self.current_model_index]

    def _call_model(self, model: str, payload: Dict[str, Any], timeout: int = 60) -> Optional[str]:
        """Appel brut à l'API Gemini. Retourne le texte de réponse, 'QUOTA_EXCEEDED' ou None."""
        if not self.enabled:
            return None
        url = f"{self.base_url}/models/{model}:generateContent"
        params = {"key": self.api_key}
        try:
            resp = requests.post(url, params=params, json=payload, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                candidates = data.get("candidates", [])
                if candidates:
                    content = candidates[0].get("content", {})
                    parts = content.get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                    return text
                return None
            elif resp.status_code == 429:
                logger.warning(f"Quota atteint sur modèle {model} (HTTP 429), fallback...")
                return "QUOTA_EXCEEDED"
            else:
                logger.warning(f"Erreur HTTP {resp.status_code} sur modèle {model}: {resp.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"Erreur appel Gemini {model}: {e}")
            return None

    def _generate_with_fallback(self, payload: Dict[str, Any], max_retries: int = 2) -> Optional[str]:
        """Appelle les modèles en cascade jusqu'à obtenir une réponse valide."""
        if not self.enabled:
            return None
        attempts = 0
        total_models = len(self.models)
        for _ in range(total_models * max_retries):
            model = self.models[self.current_model_index]
            result = self._call_model(model, payload)
            if result is None:
                self.current_model_index = (self.current_model_index + 1) % total_models
                attempts += 1
                time.sleep(1.5 * (attempts + 1))
                continue
            if result == "QUOTA_EXCEEDED":
                self.current_model_index = (self.current_model_index + 1) % total_models
                attempts += 1
                time.sleep(2)
                continue
            self.current_model_index = 0
            return result
        logger.error("Tous les modèles Gemini ont échoué (quota ou indisponibilité)")
        return None

    def _build_text_payload(self, prompt: str) -> Dict[str, Any]:
        return {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024}
        }

    def _build_vision_payload(self, prompt: str, image_bytes: bytes,
                              mime_type: str = "image/jpeg") -> Dict[str, Any]:
        b64_image = base64.b64encode(image_bytes).decode("utf-8")
        return {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": mime_type, "data": b64_image}}
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1024}
        }

    def _parse_json_response(self, result: str) -> Optional[Dict[str, Any]]:
        """Extraction robuste du JSON depuis la réponse Gemini."""
        if not result:
            return None
        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[1].rsplit("```", 1)[0]
            return json.loads(result)
        except Exception as e:
            logger.error(f"Erreur parsing réponse Gemini: {e}, raw: {result[:200]}")
            return None

    def analyze_listing_text(self, title: str, description: str, requirements: str,
                             keywords: List[str]) -> Optional[Dict[str, Any]]:
        """Étape A - Filtre textuel : analyse titre + description complète."""
        prompt = (
            "Tu es un assistant d'analyse d'annonces Vinted rigoureux.\n"
            f"Produit recherché : {requirements}\n"
            f"Mots-clés de recherche utilisés : {', '.join(keywords)}\n\n"
            f"Titre de l'annonce : {title}\n"
            f"Description : {description}\n\n"
            "Règle de TOLÉRANCE ZÉRO SUR LE DOUTE : si les spécifications techniques "
            "exigées ne sont pas clairement respectées, ou en cas d'ambiguïté persistante, "
            "la décision doit être 'rejected'.\n\n"
            "Réponds UNIQUEMENT en JSON valide avec ce format exact :\n"
            '{"decision": "validated"|"rejected"|"uncertain", "reason": "courte justification technique"}'
        )
        payload = self._build_text_payload(prompt)
        result = self._generate_with_fallback(payload)
        if not result:
            return None
        data = self._parse_json_response(result)
        if data and data.get("decision") in ("validated", "rejected", "uncertain"):
            return data
        return None

    def analyze_listing_vision(self, title: str, description: str, requirements: str,
                               image_bytes: bytes) -> Optional[Dict[str, Any]]:
        """Étape B - Levée de doute par Vision : analyse la photo principale."""
        prompt = (
            "Tu es un assistant d'analyse d'annonces Vinted rigoureux, spécialisé "
            "dans l'inspection visuelle (lecture d'étiquette, vérification de produit).\n"
            f"Produit recherché : {requirements}\n"
            f"Titre de l'annonce : {title}\n"
            f"Description : {description}\n\n"
            "Examine attentivement la photo. Applique la règle de TOLÉRANCE ZÉRO SUR LE DOUTE : "
            "si le produit visible ne correspond pas clairement aux spécifications exigées, "
            "ou en cas d'ambiguïté persistante, la décision doit être 'rejected'.\n\n"
            "Réponds UNIQUEMENT en JSON valide avec ce format exact :\n"
            '{"decision": "validated"|"rejected"|"uncertain", "reason": "courte justification technique"}'
        )
        payload = self._build_vision_payload(prompt, image_bytes)
        result = self._generate_with_fallback(payload)
        if not result:
            return None
        data = self._parse_json_response(result)
        if data and data.get("decision") in ("validated", "rejected", "uncertain"):
            return data
        return None

    def generate_keywords(self, product_description: str, strict_requirements: str) -> Optional[Dict[str, List[str]]]:
        """Assistant IA de mots-clés : génère mots-clés élargis + blacklist initiale."""
        prompt = (
            "Tu es un expert en recherche de produits sur Vinted.\n"
            f"Produit cible : {product_description}\n"
            f"Exigences strictes : {strict_requirements}\n\n"
            "Génère et propose simultanément :\n"
            "1. Une liste de mots-clés de recherche élargis (synonymes, fautes fréquentes, termes génériques).\n"
            "2. Une liste initiale de mots-clés d'exclusion (Blacklist) (ex: HS, pour pièces, boîte vide, câble seul).\n\n"
            "Réponds UNIQUEMENT en JSON valide avec ce format exact :\n"
            '{"keywords": ["mot1", "mot2", ...], "blacklist": ["excl1", "excl2", ...]}'
        )
        payload = self._build_text_payload(prompt)
        result = self._generate_with_fallback(payload)
        if not result:
            return None
        data = self._parse_json_response(result)
        if data and "keywords" in data and "blacklist" in data:
            return data
        return None

    def generate_market_report(self, listings_stats: Dict[str, Any],
                               product_description: str) -> Optional[str]:
        """Génère le compte-rendu complet de la Phase 1."""
        stats = listings_stats
        prompt = (
            "Tu es un analyste de marché Vinted expert.\n"
            f"Produit : {product_description}\n\n"
            f"Données statistiques de la Phase 1 :\n"
            f"- Nombre total d'annonces observées : {stats.get('total', 0)}\n"
            f"- Annonces validées et vendues : {stats.get('sold_validated_count', 0)}\n"
            f"- Prix moyen de vente : {stats.get('avg_price', 'N/A')} €\n"
            f"- Prix minimum : {stats.get('min_price', 'N/A')} €\n"
            f"- Prix maximum : {stats.get('max_price', 'N/A')} €\n"
            f"- Délai moyen de vente : {stats.get('avg_sale_time_hours', 'N/A')} heures\n"
            f"- Annonces supprimées (deleted) : {stats.get('deleted_count', 0)} "
            f"({stats.get('deleted_pct', 'N/A')}%)\n\n"
            "Rédige un compte-rendu complet en français incluant :\n"
            "1. Prix moyen de vente et prix plancher / bonne affaire.\n"
            "2. Temps moyen de vente.\n"
            "3. Analyse sémantique des descriptions avec suggestions de mots-clés "
            "positifs ou négatifs à ajuster.\n\n"
            "Réponds uniquement par le compte-rendu rédigé, sans JSON."
        )
        payload = self._build_text_payload(prompt)
        return self._generate_with_fallback(payload)

    def is_available(self) -> bool:
        return self.enabled and len(self.models) > 0