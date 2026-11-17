#!/usr/bin/env python3
"""Surveillance Philibert des articles « Pokémon 30 ans » disponibles à l'achat.

Le filtre affiché sur la page catégorie Pokémon
(https://www.philibertnet.com/fr/212-pokemon#e210/mobile/m=and&q=pokémon+30+ans)
est un calque de recherche Doofinder (m=and → match_and). Le site HTML est
protégé par Akamai Bot Manager ; ce script interroge directement l'API de
recherche publique utilisée par ce calque, ce qui renvoie id, nom, prix, URL
et disponibilité au format JSON.

Un article est considéré comme achetable s'il est en stock ou en précommande
effective (ajout au panier possible). Les fiches « à venir » encore marquées
indisponibles sont mémorisées mais ne déclenchent pas d'alerte, jusqu'à ce
qu'elles passent à l'achat.

La première exécution enregistre seulement une baseline, sauf --alert-on-first-run.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Paris")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Moteur Doofinder public de philibertnet.com (zone EU, calque classique).
DEFAULT_HASHID = "b220561599a93dfc2d82f89bf6223e54"
DEFAULT_ZONE = "eu1"
DEFAULT_QUERY = "pokémon 30 ans"
DEFAULT_QUERY_NAME = "match_and"
DEFAULT_INTERVAL = 90  # secondes — voir README
MIN_INTERVAL = 30
DEFAULT_RPP = 100
DEFAULT_STATE_FILE = "seen_products.json"
PAGE_URL = (
    "https://www.philibertnet.com/fr/212-pokemon"
    "#e210/mobile/m=and&q=pok%C3%A9mon+30+ans"
)

# Disponibilités clairement achetables / non achetables côté index Doofinder.
PURCHASABLE_STATUSES = {
    "en stock",
    "précommande",
    "precommande",
    "sur commande",
    "disponible",
    "derniers exemplaires",
    "en arrivage",
    "stock limité",
}
UNAVAILABLE_STATUSES = {
    "indisponible",
    "rupture de stock",
    "rupture",
    "épuisé",
    "epuise",
    "hors stock",
}

logger = logging.getLogger("philibert-watch")


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


def load_dotenv(path: Path) -> None:
    """Charge un fichier .env simple sans écraser l'environnement déjà défini."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Product:
    id: str
    name: str
    url: str
    price: float | None
    availability: str
    purchasable: bool
    preorder: bool
    stock_quantity: int | None

    def price_label(self) -> str:
        if self.price is None:
            return "prix non affiché"
        formatted = f"{self.price:.2f}".replace(".", ",")
        return f"{formatted} €"


def is_preorder(raw: dict[str, Any]) -> bool:
    product_type = str(raw.get("product_type") or "").strip().lower()
    if product_type in {"preorder", "précommande", "precommande"}:
        return True
    availability = str(raw.get("availability") or "").strip().lower()
    if "précommande" in availability or "precommande" in availability:
        return True
    categories = raw.get("categories") or []
    for cat in categories:
        text = str(cat).casefold()
        if "précommande" in text or "precommande" in text:
            return True
    return False


def is_purchasable(raw: dict[str, Any]) -> bool:
    """True si l'article peut être mis au panier (stock ou précommande ouverte)."""
    availability = str(raw.get("availability") or "").strip().lower()
    if availability in PURCHASABLE_STATUSES:
        return True
    if availability in UNAVAILABLE_STATUSES:
        return False

    stock = raw.get("stock_quantity")
    if isinstance(stock, (int, float)) and stock > 0:
        return True

    price = raw.get("price")
    if price is None:
        price = raw.get("best_price")
    if price not in (None, "", 0, 0.0) and availability:
        return True
    return False


def product_from_raw(raw: dict[str, Any]) -> Product:
    product_id = str(raw.get("id") or "").strip()
    name = str(raw.get("title") or raw.get("name") or "").strip()
    url = str(raw.get("link") or raw.get("url") or "").strip()
    price = raw.get("price")
    if price is None:
        price = raw.get("best_price")
    try:
        price_f = float(price) if price not in (None, "") else None
    except (TypeError, ValueError):
        price_f = None
    stock = raw.get("stock_quantity")
    stock_i: int | None
    try:
        stock_i = int(stock) if stock is not None else None
    except (TypeError, ValueError):
        stock_i = None
    return Product(
        id=product_id,
        name=name,
        url=url,
        price=price_f,
        availability=str(raw.get("availability") or "").strip() or "inconnu",
        purchasable=is_purchasable(raw),
        preorder=is_preorder(raw),
        stock_quantity=stock_i,
    )


def status_label(product: Product) -> str:
    if product.preorder and product.purchasable:
        return f"Précommande — {product.availability}"
    if product.preorder and not product.purchasable:
        return f"À venir (pas encore achetable) — {product.availability}"
    if product.purchasable:
        return product.availability or "Achetable"
    return f"Non achetable — {product.availability}"


def fetch_page(
    hashid: str,
    zone: str,
    query: str,
    query_name: str,
    page: int,
    rpp: int,
    timeout: int,
) -> dict[str, Any]:
    base = f"https://{zone}-search.doofinder.com/5/search"
    params = {
        "hashid": hashid,
        "query": query,
        "query_name": query_name,
        "rpp": str(rpp),
        "page": str(page),
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Origin": "https://www.philibertnet.com",
            "Referer": "https://www.philibertnet.com/fr/",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} depuis Doofinder : {body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Erreur réseau Doofinder : {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Réponse Doofinder inattendue (pas un objet JSON).")
    if payload.get("error"):
        raise RuntimeError(f"Doofinder a renvoyé une erreur : {payload['error']}")
    return payload


def fetch_products(
    hashid: str,
    zone: str,
    query: str,
    query_name: str,
    timeout: int = 20,
) -> list[Product]:
    products: list[Product] = []
    seen_ids: set[str] = set()
    page = 1
    total_found: int | None = None
    while True:
        payload = fetch_page(hashid, zone, query, query_name, page, DEFAULT_RPP, timeout)
        raw_results = payload.get("results") or []
        total_found = int(payload.get("total_found") or payload.get("total") or 0)
        rpp = int(payload.get("results_per_page") or DEFAULT_RPP) or DEFAULT_RPP
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            product = product_from_raw(raw)
            if not product.id or product.id in seen_ids:
                continue
            seen_ids.add(product.id)
            products.append(product)
        if page * rpp >= total_found or not raw_results:
            break
        page += 1
        if page > 50:
            logger.warning("Pagination interrompue après 50 pages.")
            break
    products.sort(key=lambda item: (not item.purchasable, item.name.casefold()))
    logger.debug("Doofinder total_found=%s récupérés=%s", total_found, len(products))
    return products


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"products": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Impossible de lire %s (%s) ; baseline recréée.", path, exc)
        return {"products": {}}
    if not isinstance(data, dict):
        return {"products": {}}
    products = data.get("products")
    if not isinstance(products, dict):
        data["products"] = {}
    return data


def save_state(path: Path, query: str, products: list[Product], previous: dict[str, Any]) -> None:
    existing = previous.get("products") or {}
    snapshot: dict[str, Any] = {}
    for product in products:
        prior = existing.get(product.id) if isinstance(existing.get(product.id), dict) else {}
        first_seen = prior.get("first_seen") or now_iso()
        snapshot[product.id] = {
            **asdict(product),
            "first_seen": first_seen,
            "last_seen": now_iso(),
        }
    payload = {
        "query": query,
        "updated_at": now_iso(),
        "products": snapshot,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def detect_alerts(
    products: list[Product],
    previous: dict[str, Any],
    alert_on_first_run: bool,
) -> tuple[list[tuple[str, Product]], bool]:
    """Retourne (alertes, est_baseline).

    Chaque alerte est un couple (raison, produit) avec raison
    « nouveau » ou « de nouveau achetable ».
    """
    existing = previous.get("products") or {}
    first_run = not bool(existing)
    alerts: list[tuple[str, Product]] = []
    if first_run and not alert_on_first_run:
        return [], True

    for product in products:
        if not product.purchasable:
            continue
        prior = existing.get(product.id)
        if not prior:
            alerts.append(("nouveau", product))
            continue
        was_purchasable = bool(prior.get("purchasable")) if isinstance(prior, dict) else False
        if not was_purchasable:
            alerts.append(("de nouveau achetable", product))
    return alerts, first_run


def format_product_line(product: Product, reason: str | None = None) -> str:
    prefix = f"[{reason}] " if reason else ""
    preorder = " (précommande)" if product.preorder and product.purchasable else ""
    return (
        f"{prefix}{product.name}{preorder}\n"
        f"    {status_label(product)} — {product.price_label()}\n"
        f"    {product.url}"
    )


def build_email(alerts: list[tuple[str, Product]], query: str) -> tuple[str, str]:
    n = len(alerts)
    if n == 1:
        reason, product = alerts[0]
        subject = f"[Philibert] {reason.capitalize()} : {product.name}"
    else:
        subject = f"[Philibert] {n} articles « {query} » maintenant achetables"

    lines = [
        f"Surveillance Philibert — requête « {query} ».",
        f"Page : {PAGE_URL}",
        f"Détecté à {now_local().strftime('%d/%m/%Y %H:%M:%S')} (Europe/Paris).",
        "",
    ]
    for reason, product in alerts:
        lines.append(format_product_line(product, reason))
        lines.append("")
    lines.append("— script watch_philibert.py")
    return subject, "\n".join(lines)


def gmail_credentials() -> tuple[str, str, str]:
    address = (os.environ.get("GMAIL_ADDRESS") or "").strip()
    password = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "").strip()
    mail_to = (os.environ.get("MAIL_TO") or address).strip()
    missing = []
    if not address:
        missing.append("GMAIL_ADDRESS")
    if not password:
        missing.append("GMAIL_APP_PASSWORD")
    if missing:
        raise RuntimeError(
            "Variables d'environnement manquantes pour l'e-mail : "
            + ", ".join(missing)
            + ". Voir .env.example."
        )
    return address, password, mail_to


def send_gmail(subject: str, body: str) -> None:
    address, password, mail_to = gmail_credentials()
    message = EmailMessage()
    message["From"] = address
    message["To"] = mail_to
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(address, password)
        smtp.send_message(message)
    logger.info("E-mail envoyé à %s : %s", mail_to, subject)


def next_sleep(interval: int, backoff: float) -> float:
    base = max(interval, MIN_INTERVAL) * backoff
    # Jitter de ±20 % pour éviter un rythme parfaitement régulier.
    jitter = random.uniform(0.8, 1.2)
    return max(MIN_INTERVAL, base * jitter)


def run_once(
    args: argparse.Namespace,
    previous: dict[str, Any] | None = None,
) -> tuple[list[Product], list[tuple[str, Product]], bool]:
    products = fetch_products(
        hashid=args.hashid,
        zone=args.zone,
        query=args.query,
        query_name=args.query_name,
    )
    state = previous if previous is not None else load_state(args.state_file)
    alerts, first_run = detect_alerts(products, state, args.alert_on_first_run)
    purchasable = [p for p in products if p.purchasable]
    logger.info(
        "%s article(s) pour « %s » — %s achetable(s), %s non achetable(s).",
        len(products),
        args.query,
        len(purchasable),
        len(products) - len(purchasable),
    )
    for product in products:
        logger.info("  • %s", format_product_line(product).replace("\n    ", " | "))
    if first_run and not args.alert_on_first_run:
        logger.info(
            "Première exécution : baseline enregistrée dans %s, aucune alerte envoyée.",
            args.state_file,
        )
    elif alerts:
        logger.info("%s alerte(s) :", len(alerts))
        for reason, product in alerts:
            logger.info("  ! %s", format_product_line(product, reason).replace("\n    ", " | "))
    else:
        logger.info("Aucun nouvel article achetable depuis la dernière passe.")

    if alerts and not args.dry_run:
        subject, body = build_email(alerts, args.query)
        send_gmail(subject, body)
    elif alerts and args.dry_run:
        logger.info("Mode --dry-run : e-mail non envoyé.")

    save_state(args.state_file, args.query, products, state)
    return products, alerts, first_run


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Surveille la recherche Philibert « pokémon 30 ans » et envoie un "
            "e-mail Gmail quand un nouvel article devient achetable."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Effectue un seul passage puis quitte.",
    )
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Envoie un e-mail de test via Gmail puis quitte (sans interroger Philibert).",
    )
    parser.add_argument(
        "--alert-on-first-run",
        action="store_true",
        help="Alerte aussi pour les articles déjà achetables lors de la première passe.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Détecte les alertes mais n'envoie aucun e-mail.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL", DEFAULT_INTERVAL)),
        help=f"Intervalle de sondage en secondes (défaut : {DEFAULT_INTERVAL}).",
    )
    parser.add_argument(
        "--query",
        default=os.environ.get("PHILIBERT_QUERY", DEFAULT_QUERY),
        help=f"Requête Doofinder (défaut : « {DEFAULT_QUERY} »).",
    )
    parser.add_argument(
        "--query-name",
        default=DEFAULT_QUERY_NAME,
        help=f"Mode de correspondance Doofinder (défaut : {DEFAULT_QUERY_NAME}).",
    )
    parser.add_argument(
        "--hashid",
        default=os.environ.get("DOOFINDER_HASHID", DEFAULT_HASHID),
        help="Identifiant du moteur Doofinder Philibert.",
    )
    parser.add_argument(
        "--zone",
        default=os.environ.get("DOOFINDER_ZONE", DEFAULT_ZONE),
        help="Zone Doofinder (défaut : eu1).",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)),
        help="Fichier JSON mémorisant les articles déjà vus.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Logs plus détaillés.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    script_dir = Path(__file__).resolve().parent
    load_dotenv(script_dir / ".env")
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def _local_converter(timestamp):
        return datetime.fromtimestamp(timestamp, LOCAL_TZ).timetuple()

    for handler in logging.getLogger().handlers:
        if handler.formatter is not None:
            handler.formatter.converter = _local_converter

    if args.test_email:
        subject = "[Philibert] Test de l'alerte e-mail"
        body = (
            "Ceci est un e-mail de test envoyé par watch_philibert.py.\n"
            f"Envoyé à {now_local().strftime('%d/%m/%Y %H:%M:%S')} (Europe/Paris).\n"
            f"Page surveillée : {PAGE_URL}\n"
        )
        try:
            send_gmail(subject, body)
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1
        return 0

    if args.interval < MIN_INTERVAL:
        logger.warning(
            "Intervalle %ss trop court, relevé à %ss pour rester raisonnable.",
            args.interval,
            MIN_INTERVAL,
        )
        args.interval = MIN_INTERVAL

    if args.once:
        run_once(args)
        return 0

    logger.info(
        "Surveillance lancée (intervalle %ss ±20 %% de jitter) — requête « %s ».",
        args.interval,
        args.query,
    )
    backoff = 1.0
    max_backoff = 16.0  # 90s * 16 * jitter ≈ 20 min max
    while True:
        try:
            run_once(args)
            backoff = 1.0
        except KeyboardInterrupt:
            logger.info("Arrêt demandé.")
            return 0
        except Exception as exc:  # noqa: BLE001 — boucle longue, on journalise et on réessaie
            backoff = min(max_backoff, backoff * 2)
            logger.error(
                "Échec du passage (%s). Prochain essai avec backoff x%s.",
                exc,
                int(backoff),
            )
        sleep_for = next_sleep(args.interval, backoff)
        logger.info("Prochaine vérification dans %.0f s.", sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            logger.info("Arrêt demandé.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
