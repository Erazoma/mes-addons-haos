"""Module de gestion des bases de données SQLite pour l'add-on Vinted Market Intelligence."""
import sqlite3
import json
import os
import logging
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get("HAOS_DATA_DIR", "/data")
OBSERVATORY_DB = os.path.join(DATA_DIR, "observatory.db")
SNIPER_AUDIT_DB = os.path.join(DATA_DIR, "sniper_audit.db")


def get_db_path(db_name: str) -> str:
    return os.path.join(DATA_DIR, f"{db_name}.db")


@contextmanager
def get_connection(db_path: str):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Erreur base de données {db_path}: {e}")
        raise
    finally:
        conn.close()


def init_observatory_db():
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS listings (
                vinted_id INTEGER PRIMARY KEY,
                search_id INTEGER NOT NULL,
                keyword_used TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                category TEXT,
                brand TEXT,
                condition TEXT,
                initial_price REAL NOT NULL,
                price_history TEXT NOT NULL DEFAULT '[]',
                final_price REAL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at_vinted TEXT,
                first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ended_at TEXT,
                url TEXT NOT NULL,
                image_url TEXT,
                local_image_path TEXT,
                ai_status TEXT NOT NULL DEFAULT 'pending',
                ai_reason TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_search_id ON listings(search_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_ai_status ON listings(ai_status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_keyword ON listings(keyword_used)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings(first_seen_at)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                product_description TEXT NOT NULL,
                strict_requirements TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                blacklist TEXT NOT NULL DEFAULT '[]',
                phase1_duration_days INTEGER NOT NULL DEFAULT 7,
                max_price_phase2 REAL,
                status TEXT NOT NULL DEFAULT 'draft',
                phase1_started_at TEXT,
                phase1_ended_at TEXT,
                phase2_started_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ai_report TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_searches_status ON searches(status)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS scan_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER NOT NULL,
                keyword_used TEXT NOT NULL,
                listings_found INTEGER NOT NULL DEFAULT 0,
                listings_new INTEGER NOT NULL DEFAULT 0,
                listings_updated INTEGER NOT NULL DEFAULT 0,
                error_message TEXT,
                scanned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_scan_logs_search ON scan_logs(search_id)")
        logger.info("Base de données observatory.db initialisée")


def init_sniper_audit_db():
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS sniper_audit (
                vinted_id INTEGER PRIMARY KEY,
                search_id INTEGER NOT NULL,
                keyword_used TEXT NOT NULL,
                title TEXT NOT NULL,
                last_seen_price REAL NOT NULL,
                ai_decision TEXT NOT NULL,
                ai_reason TEXT,
                notified_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sniper_search ON sniper_audit(search_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sniper_decision ON sniper_audit(ai_decision)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sniper_keyword ON sniper_audit(keyword_used)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sniper_notified ON sniper_audit(notified_at)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vinted_id INTEGER NOT NULL,
                search_id INTEGER NOT NULL,
                alert_type TEXT NOT NULL,
                price REAL NOT NULL,
                price_drop_pct REAL,
                message_sent INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_search ON alerts(search_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_vinted ON alerts(vinted_id)")
        logger.info("Base de données sniper_audit.db initialisée")


def init_all_databases():
    os.makedirs(DATA_DIR, exist_ok=True)
    init_observatory_db()
    init_sniper_audit_db()


# ============================================================
# OPÉRATIONS SUR LA TABLE SEARCHES
# ============================================================

def create_search(name: str, product_description: str, strict_requirements: str,
                  keywords: list, blacklist: list, phase1_duration_days: int,
                  max_price_phase2: float = None) -> int:
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO searches (name, product_description, strict_requirements, keywords, blacklist,
                                  phase1_duration_days, max_price_phase2, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')
        """, (name, product_description, strict_requirements, json.dumps(keywords),
              json.dumps(blacklist), phase1_duration_days, max_price_phase2))
        return c.lastrowid


def get_search(search_id: int):
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM searches WHERE id = ?", (search_id,))
        row = c.fetchone()
        if row:
            result = dict(row)
            result['keywords'] = json.loads(result['keywords'])
            result['blacklist'] = json.loads(result['blacklist'])
            if result['ai_report']:
                result['ai_report'] = json.loads(result['ai_report'])
            return result
        return None


def get_all_searches():
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM searches ORDER BY created_at DESC")
        results = []
        for row in c.fetchall():
            result = dict(row)
            result['keywords'] = json.loads(result['keywords'])
            result['blacklist'] = json.loads(result['blacklist'])
            if result['ai_report']:
                result['ai_report'] = json.loads(result['ai_report'])
            results.append(result)
        return results


def update_search(search_id: int, **kwargs) -> bool:
    allowed_fields = ['name', 'product_description', 'strict_requirements', 'keywords', 'blacklist',
                      'phase1_duration_days', 'max_price_phase2', 'status', 'phase1_started_at',
                      'phase1_ended_at', 'phase2_started_at', 'ai_report']
    updates = []
    values = []
    for key, value in kwargs.items():
        if key in allowed_fields:
            if key in ['keywords', 'blacklist', 'ai_report'] and value is not None:
                value = json.dumps(value)
            updates.append(f"{key} = ?")
            values.append(value)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    values.append(search_id)
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute(f"UPDATE searches SET {', '.join(updates)} WHERE id = ?", values)
        return c.rowcount > 0


# ============================================================
# OPÉRATIONS SUR LA TABLE LISTINGS (observatory.db)
# ============================================================

def upsert_listing(listing: dict) -> bool:
    """Insère ou met à jour une annonce. Retourne True si nouvelle annonce."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT vinted_id, price_history, status FROM listings WHERE vinted_id = ?",
                  (listing['vinted_id'],))
        existing = c.fetchone()
        now = datetime.utcnow().isoformat()
        price_history = json.dumps([{"price": listing['initial_price'], "timestamp": now}])

        if existing:
            old_history = json.loads(existing['price_history'])
            old_history.append({"price": listing['initial_price'], "timestamp": now})
            new_status = listing.get('status', 'active')
            if existing['status'] == 'active' and new_status in ('sold', 'deleted'):
                c.execute("""
                    UPDATE listings SET price_history = ?, final_price = ?, status = ?, ended_at = ?,
                                        title = ?, description = ?, category = ?, brand = ?, condition = ?,
                                        url = ?, image_url = ?, local_image_path = ?
                    WHERE vinted_id = ?
                """, (json.dumps(old_history), listing['initial_price'], new_status, now,
                      listing['title'], listing.get('description', ''), listing.get('category', ''),
                      listing.get('brand', ''), listing.get('condition', ''), listing['url'],
                      listing.get('image_url', ''), listing.get('local_image_path', ''),
                      listing['vinted_id']))
            else:
                c.execute("""
                    UPDATE listings SET price_history = ?, status = ?,
                                        title = ?, description = ?, category = ?, brand = ?, condition = ?,
                                        url = ?, image_url = ?, local_image_path = ?
                    WHERE vinted_id = ?
                """, (json.dumps(old_history), new_status,
                      listing['title'], listing.get('description', ''), listing.get('category', ''),
                      listing.get('brand', ''), listing.get('condition', ''), listing['url'],
                      listing.get('image_url', ''), listing.get('local_image_path', ''),
                      listing['vinted_id']))
            return False
        else:
            c.execute("""
                INSERT INTO listings (vinted_id, search_id, keyword_used, title, description,
                                     category, brand, condition, initial_price, price_history,
                                     final_price, status, created_at_vinted, first_seen_at,
                                     ended_at, url, image_url, local_image_path, ai_status, ai_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (listing['vinted_id'], listing['search_id'], listing['keyword_used'],
                  listing['title'], listing.get('description', ''), listing.get('category', ''),
                  listing.get('brand', ''), listing.get('condition', ''), listing['initial_price'],
                  price_history, listing.get('final_price'), listing.get('status', 'active'),
                  listing.get('created_at_vinted'), now, listing.get('ended_at'),
                  listing['url'], listing.get('image_url', ''), listing.get('local_image_path', ''),
                  listing.get('ai_status', 'pending'), listing.get('ai_reason', '')))
            return True


def get_pending_listings(search_id: int, limit: int = 50):
    """Récupère les annonces en attente d'analyse IA pour une recherche."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM listings
            WHERE search_id = ? AND ai_status = 'pending'
            ORDER BY first_seen_at ASC LIMIT ?
        """, (search_id, limit))
        return [dict(row) for row in c.fetchall()]


def update_listing_ai_status(vinted_id: int, ai_status: str, ai_reason: str = ""):
    """Met à jour le statut IA d'une annonce."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("UPDATE listings SET ai_status = ?, ai_reason = ? WHERE vinted_id = ?",
                  (ai_status, ai_reason, vinted_id))


def get_validated_sold_listings(search_id: int):
    """Récupère les annonces validées ET vendues pour les statistiques."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT * FROM listings
            WHERE search_id = ? AND ai_status = 'validated' AND status = 'sold'
            ORDER BY ended_at ASC
        """, (search_id,))
        return [dict(row) for row in c.fetchall()]


def get_all_listings_for_search(search_id: int):
    """Récupère toutes les annonces d'une recherche."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM listings WHERE search_id = ? ORDER BY first_seen_at DESC", (search_id,))
        return [dict(row) for row in c.fetchall()]


def get_listing_stats(search_id: int) -> dict:
    """Récupère les statistiques d'une recherche pour le dashboard."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) as total FROM listings WHERE search_id = ?", (search_id,))
        total = c.fetchone()['total']
        c.execute("SELECT status, COUNT(*) as count FROM listings WHERE search_id = ? GROUP BY status", (search_id,))
        by_status = {row['status']: row['count'] for row in c.fetchall()}
        c.execute("SELECT ai_status, COUNT(*) as count FROM listings WHERE search_id = ? GROUP BY ai_status", (search_id,))
        by_ai_status = {row['ai_status']: row['count'] for row in c.fetchall()}
        c.execute("""
            SELECT AVG(final_price) as avg_price, MIN(final_price) as min_price,
                   MAX(final_price) as max_price, COUNT(*) as count
            FROM listings
            WHERE search_id = ? AND ai_status = 'validated' AND status = 'sold' AND final_price IS NOT NULL
        """, (search_id,))
        price_stats = dict(c.fetchone())
        return {'total': total, 'by_status': by_status, 'by_ai_status': by_ai_status, 'price_stats': price_stats}


def log_scan(search_id: int, keyword_used: str, listings_found: int,
             listings_new: int, listings_updated: int, error: str = None):
    """Enregistre un log de scan."""
    with get_connection(OBSERVATORY_DB) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO scan_logs (search_id, keyword_used, listings_found, listings_new,
                                   listings_updated, error_message)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (search_id, keyword_used, listings_found, listings_new, listings_updated, error))


# ============================================================
# OPÉRATIONS SUR SNIPER_AUDIT (sniper_audit.db)
# ============================================================

def upsert_sniper_audit(audit: dict) -> bool:
    """Insère ou met à jour un enregistrement dans sniper_audit.db. Retourne True si nouvelle entrée."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT vinted_id, last_seen_price, ai_decision FROM sniper_audit WHERE vinted_id = ?",
                  (audit['vinted_id'],))
        existing = c.fetchone()
        now = datetime.utcnow().isoformat()

        if existing:
            c.execute("""
                UPDATE sniper_audit SET last_seen_price = ?, ai_decision = ?, ai_reason = ?,
                                        updated_at = ?
                WHERE vinted_id = ?
            """, (audit['last_seen_price'], audit['ai_decision'], audit.get('ai_reason', ''), now,
                  audit['vinted_id']))
            return False
        else:
            c.execute("""
                INSERT INTO sniper_audit (vinted_id, search_id, keyword_used, title, last_seen_price,
                                         ai_decision, ai_reason, notified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (audit['vinted_id'], audit['search_id'], audit['keyword_used'], audit['title'],
                  audit['last_seen_price'], audit['ai_decision'], audit.get('ai_reason', ''),
                  audit.get('notified_at')))
            return True


def get_sniper_audit(vinted_id: int):
    """Récupère un enregistrement d'audit par son ID Vinted."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM sniper_audit WHERE vinted_id = ?", (vinted_id,))
        row = c.fetchone()
        return dict(row) if row else None


def get_sniper_stats(search_id: int) -> dict:
    """Récupère les statistiques du sniper pour une recherche."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT keyword_used, ai_decision, COUNT(*) as count
            FROM sniper_audit
            WHERE search_id = ?
            GROUP BY keyword_used, ai_decision
        """, (search_id,))
        by_keyword = {}
        for row in c.fetchall():
            kw = row['keyword_used']
            if kw not in by_keyword:
                by_keyword[kw] = {'retained': 0, 'rejected': 0}
            by_keyword[kw][row['ai_decision']] = row['count']
        c.execute("""
            SELECT COUNT(*) as total_alerts,
                   SUM(CASE WHEN alert_type = 'price_drop' THEN 1 ELSE 0 END) as price_drops
            FROM alerts WHERE search_id = ?
        """, (search_id,))
        alert_stats = dict(c.fetchone())
        return {'by_keyword': by_keyword, 'alerts': alert_stats}


def create_alert(vinted_id: int, search_id: int, alert_type: str, price: float,
                 price_drop_pct: float = None) -> int:
    """Crée une nouvelle alerte."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO alerts (vinted_id, search_id, alert_type, price, price_drop_pct)
            VALUES (?, ?, ?, ?, ?)
        """, (vinted_id, search_id, alert_type, price, price_drop_pct))
        return c.lastrowid


def mark_alert_sent(alert_id: int):
    """Marque une alerte comme envoyée."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("UPDATE alerts SET message_sent = 1, sent_at = ? WHERE id = ?",
                  (datetime.utcnow().isoformat(), alert_id))


def get_unsent_alerts(limit: int = 10):
    """Récupère les alertes non envoyées."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("""
            SELECT a.*, sa.title
            FROM alerts a
            JOIN sniper_audit sa ON a.vinted_id = sa.vinted_id
            WHERE a.message_sent = 0
            ORDER BY a.created_at ASC
            LIMIT ?
        """, (limit,))
        return [dict(row) for row in c.fetchall()]


def update_sniper_notified(vinted_id: int):
    """Met à jour la date de notification dans sniper_audit."""
    with get_connection(SNIPER_AUDIT_DB) as conn:
        c = conn.cursor()
        c.execute("UPDATE sniper_audit SET notified_at = ? WHERE vinted_id = ?",
                  (datetime.utcnow().isoformat(), vinted_id))