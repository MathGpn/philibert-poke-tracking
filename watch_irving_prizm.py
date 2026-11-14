#!/usr/bin/env python3
"""Surveillance Vinted pour une carte Kyrie Irving rookie Panini Prizm.

Interroge le catalogue public de Vinted (api.{domaine}/svc-catalogue/items)
toutes les 10 minutes par défaut, filtre les titres plausibles, et n'affiche
que les nouvelles annonces (ids déjà vus stockés dans un petit fichier JSON).

L'API interne de Vinted n'est pas documentée et peut changer sans préavis.
Aucune authentification de compte n'est requise : une session anonyme est
obtenue en visitant la page d'accueil, comme un navigateur.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

LOCAL_TZ = ZoneInfo("Europe/Paris")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Jeton CSRF actuellement embarqué dans le bundle web Vinted ; on essaie
# d'abord de le lire dynamiquement sur la page d'accueil.
FALLBACK_CSRF_TOKEN = "75f6c9fa-dc8e-4e52-a000-e09dd4084b3e"

DEFAULT_QUERIES = [
    "kyrie irving prizm rookie",
    "kyrie irving prizm 2012",
    "irving prizm 2012",
    "kyrie prizm rc",
    "kyrie irving 2012 prizm",
]
DEFAULT_NAME_KEYWORDS = ["kyrie", "irving"]
DEFAULT_SET_KEYWORDS = ["prizm", "prism"]
DEFAULT_ROOKIE_KEYWORDS = ["rookie", "rc", "2012", "2012-13", "12-13"]
# Bucky Irving (NFL) ressort souvent sur une recherche "irving prizm rc".
DEFAULT_EXCLUDE_KEYWORDS = ["bucky"]

DOMAIN_LOCALES = {
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "it": "it-IT",
    "nl": "nl-NL",
    "be": "fr-FR",
    "at": "de-AT",
    "pt": "pt-PT",
    "pl": "pl-PL",
    "cz": "cs-CZ",
    "lt": "lt-LT",
    "lu": "fr-FR",
    "com": "en-US",
    "us": "en-US",
    "co.uk": "en-GB",
    "uk": "en-GB",
    "com.au": "en-AU",
}

logger = logging.getLogger("vinted-watch")


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def format_local(dt: datetime | None = None) -> str:
    value = dt or now_local()
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    else:
        value = value.astimezone(LOCAL_TZ)
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_domain(raw: str) -> tuple[str, str, str]:
    """Retourne (www_host, api_host, locale) à partir d'un domaine configurable."""
    value = raw.strip().lower()
    value = re.sub(r"^https?://", "", value).rstrip("/")
    if value.startswith("www."):
        value = value[4:]
    if value.startswith("api."):
        value = value[4:]
    if "." not in value and not value.startswith("vinted"):
        if value in {"uk"}:
            value = "vinted.co.uk"
        elif value in {"us"}:
            value = "vinted.com"
        else:
            value = f"vinted.{value}"
    suffix = value[7:] if value.startswith("vinted.") else value
    locale = DOMAIN_LOCALES.get(suffix, "fr-FR")
    return f"www.{value}", f"api.{value}", locale


def split_keywords(raw: str | None, default: list[str]) -> list[str]:
    if raw is None:
        return list(default)
    return [part.strip().lower() for part in raw.split(",") if part.strip()]


def normalize_title(title: str) -> str:
    text = title.lower().replace("\u2019", "'")
    text = re.sub(r"[–—−]", "-", text)
    return text


def keyword_in_text(text: str, keyword: str) -> bool:
    """Correspondance souple : mots courts / codes avec frontières, sinon sous-chaîne."""
    keyword = keyword.lower().strip()
    if not keyword:
        return False
    if re.fullmatch(r"rc|ki|\d{2,4}(?:-\d{2})?", keyword):
        return re.search(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])", text) is not None
    return keyword in text


def title_matches(
    title: str,
    name_keywords: list[str],
    set_keywords: list[str],
    rookie_keywords: list[str],
    exclude_keywords: list[str],
    require_rookie: bool,
) -> bool:
    text = normalize_title(title)
    if any(keyword_in_text(text, kw) for kw in exclude_keywords):
        return False
    if not any(keyword_in_text(text, kw) for kw in name_keywords):
        return False
    if not any(keyword_in_text(text, kw) for kw in set_keywords):
        return False
    if require_rookie and not any(keyword_in_text(text, kw) for kw in rookie_keywords):
        return False
    return True


def parse_price(raw: Any) -> tuple[float | None, str]:
    if raw is None:
        return None, ""
    if isinstance(raw, dict):
        amount = raw.get("amount") or raw.get("numeric")
        currency = str(raw.get("currency_code") or raw.get("currency") or "")
        try:
            return (float(str(amount).replace(",", ".")), currency) if amount is not None else (None, currency)
        except ValueError:
            return None, currency
    try:
        return float(str(raw).replace(",", ".")), ""
    except ValueError:
        return None, ""


def format_price(amount: float | None, currency: str) -> str:
    if amount is None:
        return "?"
    currency = currency or "EUR"
    if amount == int(amount):
        return f"{int(amount)} {currency}"
    return f"{amount:.2f} {currency}"


@dataclass
class Listing:
    item_id: str
    title: str
    price_amount: float | None
    currency: str
    url: str
    discovered_at: str

    def to_seen(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "price": self.price_amount,
            "currency": self.currency,
            "url": self.url,
            "first_seen": self.discovered_at,
        }


class SeenStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Impossible de lire %s (%s) — on repart d'une liste vide.", self.path, exc)
            return
        raw_items = data.get("items", data) if isinstance(data, dict) else {}
        if isinstance(raw_items, dict):
            self.items = {str(k): v for k, v in raw_items.items()}
        elif isinstance(raw_items, list):
            self.items = {str(item_id): {} for item_id in raw_items}

    def known(self, item_id: str) -> bool:
        return item_id in self.items

    def add(self, listing: Listing) -> None:
        self.items[listing.item_id] = listing.to_seen()

    def save(self) -> None:
        payload = {
            "updated_at": format_local(),
            "items": self.items,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class VintedClient:
    def __init__(self, domain: str, locale: str | None = None, timeout: float = 30.0) -> None:
        www_host, api_host, default_locale = parse_domain(domain)
        self.www_host = www_host
        self.api_host = api_host
        self.locale = locale or default_locale
        self.timeout = timeout
        self.session = requests.Session()
        self.csrf_token = FALLBACK_CSRF_TOKEN
        self._last_bootstrap = 0.0

    @property
    def origin(self) -> str:
        return f"https://{self.www_host}"

    def bootstrap(self, force: bool = False) -> None:
        if not force and (time.time() - self._last_bootstrap) < 30:
            return
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": f"{self.locale},{self.locale.split('-')[0]};q=0.9,en;q=0.8",
        }
        response = self.session.get(self.origin + "/", headers=headers, timeout=self.timeout)
        response.raise_for_status()
        match = re.search(r'CSRF_TOKEN\\?":\\?"([0-9a-f-]{36})', response.text)
        if match:
            self.csrf_token = match.group(1)
        self._last_bootstrap = time.time()
        logger.debug(
            "Session ouverte sur %s (cookies : %s)",
            self.www_host,
            ", ".join(sorted(self.session.cookies.keys())) or "aucun",
        )

    def _api_headers(self) -> dict[str, str]:
        anon_id = self.session.cookies.get("anon_id") or ""
        return {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*, image/webp",
            "Accept-Language": f"{self.locale},{self.locale.split('-')[0]};q=0.9,en;q=0.8",
            "Origin": self.origin,
            "Referer": f"{self.origin}/catalog",
            "X-CSRF-Token": self.csrf_token,
            "X-Anon-Id": anon_id,
            "Locale": self.locale,
        }

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """GET avec retries, gestion 401/403 (nouvelle session) et 429 (backoff)."""
        timeout = kwargs.pop("timeout", self.timeout)
        headers = kwargs.pop("headers", None)
        delay = 2.0
        last_error: Exception | None = None
        for attempt in range(1, 7):
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers or self._api_headers(),
                    timeout=timeout,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                logger.warning("Erreur réseau (%s/6) : %s", attempt, exc)
                time.sleep(delay + random.uniform(0, 0.5))
                delay = min(delay * 2, 120)
                continue

            if response.status_code in (401, 403):
                logger.info("HTTP %s — renouvellement de la session anonyme.", response.status_code)
                time.sleep(min(delay, 30))
                try:
                    self.bootstrap(force=True)
                except requests.RequestException as exc:
                    logger.warning("Échec du renouvellement de session : %s", exc)
                delay = min(delay * 2, 120)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                wait = min(max(wait, 1), 300) + random.uniform(0, 1)
                logger.warning(
                    "HTTP %s — nouvel essai dans %.0f s (%s/6).",
                    response.status_code, wait, attempt,
                )
                time.sleep(wait)
                delay = min(delay * 2, 120)
                continue

            return response

        if last_error:
            raise last_error
        raise requests.HTTPError(f"Échec après plusieurs tentatives : {url}")

    def search_page(
        self,
        query: str,
        page: int = 1,
        per_page: int = 96,
        price_to: float | None = None,
        order: str = "newest_first",
    ) -> dict[str, Any]:
        self.bootstrap()
        params: dict[str, Any] = {
            "search_text": query,
            "page": page,
            "per_page": per_page,
            "order": order,
        }
        if price_to is not None:
            params["price_to"] = price_to
        url = f"https://{self.api_host}/svc-catalogue/items"
        response = self.request("GET", url, params=params)
        if response.status_code != 200:
            snippet = response.text[:300].replace("\n", " ")
            raise requests.HTTPError(
                f"Recherche Vinted HTTP {response.status_code} : {snippet}",
                response=response,
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise requests.HTTPError(
                f"Réponse non JSON ({response.status_code}) : {response.text[:200]}",
                response=response,
            ) from exc

    def search_all(
        self,
        query: str,
        per_page: int = 96,
        price_to: float | None = None,
        max_pages: int = 5,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages and page <= max_pages:
            data = self.search_page(query, page=page, per_page=per_page, price_to=price_to)
            chunk = data.get("items") or []
            items.extend(chunk)
            pagination = data.get("pagination") or {}
            total_pages = int(pagination.get("total_pages") or 1)
            logger.debug(
                "Requête %r page %s/%s — %s article(s)",
                query, page, total_pages, len(chunk),
            )
            if not chunk:
                break
            page += 1
            if page <= total_pages and page <= max_pages:
                time.sleep(0.4)
        return items


def absolutize_url(origin: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(origin + "/", path.lstrip("/"))


def listing_from_item(item: dict[str, Any], origin: str) -> Listing:
    item_id = str(item.get("id") or "")
    title = str(item.get("title") or "").strip()
    amount, currency = parse_price(item.get("price"))
    url = absolutize_url(origin, str(item.get("url") or f"/items/{item_id}"))
    return Listing(
        item_id=item_id,
        title=title,
        price_amount=amount,
        currency=currency,
        url=url,
        discovered_at=format_local(),
    )


def send_ntfy(listing: Listing) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    url = os.environ.get("NTFY_URL", "").strip()
    if not url and topic:
        url = f"https://ntfy.sh/{topic}"
    if not url:
        return
    title = "Nouvelle carte Irving Prizm"
    body = (
        f"{listing.title}\n"
        f"{format_price(listing.price_amount, listing.currency)}\n"
        f"{listing.url}\n"
        f"{listing.discovered_at}"
    )
    try:
        response = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={
                "Title": title,
                "Tags": "card,basketball",
                "Click": listing.url,
            },
            timeout=15,
        )
        if response.status_code >= 400:
            logger.warning("ntfy a répondu HTTP %s : %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        logger.warning("Notification ntfy impossible : %s", exc)


def send_telegram(listing: Listing) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    text = (
        "Nouvelle carte Irving Prizm\n"
        f"{listing.title}\n"
        f"{format_price(listing.price_amount, listing.currency)}\n"
        f"{listing.url}\n"
        f"{listing.discovered_at}"
    )
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=15,
        )
        if response.status_code >= 400:
            logger.warning("Telegram a répondu HTTP %s : %s", response.status_code, response.text[:200])
    except requests.RequestException as exc:
        logger.warning("Notification Telegram impossible : %s", exc)


def notify(listing: Listing) -> None:
    send_ntfy(listing)
    send_telegram(listing)


def print_listing(listing: Listing, prefix: str = "NOUVEAU") -> None:
    print(
        f"[{prefix}] {listing.title}\n"
        f"         prix  : {format_price(listing.price_amount, listing.currency)}\n"
        f"         url   : {listing.url}\n"
        f"         date  : {listing.discovered_at} (détection)\n"
        f"         id    : {listing.item_id}",
        flush=True,
    )


def collect_listings(client: VintedClient, args: argparse.Namespace) -> tuple[list[Listing], int]:
    seen_ids: set[str] = set()
    listings: list[Listing] = []
    raw_count = 0
    for index, query in enumerate(args.queries):
        if index:
            time.sleep(0.5)
        try:
            items = client.search_all(
                query,
                per_page=args.per_page,
                price_to=args.max_price,
                max_pages=args.max_pages,
            )
        except (requests.RequestException, ValueError) as exc:
            logger.error("Échec de la recherche %r : %s", query, exc)
            continue
        raw_count += len(items)
        for item in items:
            listing = listing_from_item(item, client.origin)
            if not listing.item_id or listing.item_id in seen_ids:
                continue
            seen_ids.add(listing.item_id)
            if not title_matches(
                listing.title,
                args.name_keywords,
                args.set_keywords,
                args.rookie_keywords,
                args.exclude_keywords,
                args.require_rookie,
            ):
                continue
            if args.max_price is not None and listing.price_amount is not None:
                if listing.price_amount > args.max_price:
                    continue
            listings.append(listing)
    listings.sort(key=lambda item: item.item_id, reverse=True)
    return listings, raw_count, len(seen_ids)


def run_once(client: VintedClient, store: SeenStore, args: argparse.Namespace) -> int:
    print(f"=== Passage {format_local()} — {client.www_host} ===", flush=True)
    listings, raw_count, unique_count = collect_listings(client, args)
    if raw_count == 0 and not listings:
        logger.error("Aucune réponse exploitable de Vinted sur ce passage.")
        return 1

    new_listings = [item for item in listings if not store.known(item.item_id)]
    print(
        f"Résultats API : {raw_count} ligne(s) au total, {unique_count} annonce(s) unique(s). "
        f"Retenues par le filtre : {len(listings)}. Nouvelles : {len(new_listings)}.",
        flush=True,
    )

    if args.show_all:
        for listing in listings:
            tag = "NOUVEAU" if listing.item_id not in store.items else "déjà vu"
            print_listing(listing, prefix=tag)

    if not new_listings and not args.show_all:
        print("Aucune nouvelle annonce.", flush=True)

    for listing in new_listings:
        if not args.show_all:
            print_listing(listing)
        notify(listing)
        store.add(listing)

    # On mémorise aussi les anciennes correspondances déjà connues (rafraîchit le titre/prix).
    for listing in listings:
        if not store.known(listing.item_id):
            store.add(listing)
        else:
            entry = store.items[listing.item_id]
            entry["title"] = listing.title
            entry["price"] = listing.price_amount
            entry["currency"] = listing.currency
            entry["url"] = listing.url

    try:
        store.save()
    except OSError as exc:
        logger.warning("Impossible d'écrire %s : %s", store.path, exc)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Surveille Vinted pour une carte Kyrie Irving rookie Panini Prizm.",
    )
    parser.add_argument(
        "--domain",
        default=os.environ.get("VINTED_DOMAIN", "vinted.fr"),
        help="Domaine Vinted (défaut : vinted.fr). Ex. vinted.fr, fr, vinted.de.",
    )
    parser.add_argument("--locale", default=None, help="Locale HTTP (défaut selon le domaine, ex. fr-FR).")
    parser.add_argument(
        "--query",
        dest="query_args",
        action="append",
        default=[],
        help="Requête de recherche (répéter l'option pour plusieurs). "
        "Si aucune n'est donnée, un jeu de requêtes par défaut est utilisé.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=10,
        help="Intervalle entre deux passages, en minutes (défaut : 10).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="N'effectuer qu'un seul passage puis quitter (utile pour tester ou un cron).",
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=None,
        help="Prix maximum (devise du site, en général EUR). Optionnel.",
    )
    parser.add_argument(
        "--seen-file",
        type=Path,
        default=Path("seen_items.json"),
        help="Fichier JSON des ids déjà vus (défaut : seen_items.json).",
    )
    parser.add_argument("--per-page", type=int, default=96, help="Nombre d'annonces par page API (défaut : 96).")
    parser.add_argument("--max-pages", type=int, default=5, help="Nombre max de pages par requête (défaut : 5).")
    parser.add_argument(
        "--name-keywords",
        default=None,
        help="Mots-clés nom, séparés par des virgules (défaut : kyrie,irving).",
    )
    parser.add_argument(
        "--set-keywords",
        default=None,
        help="Mots-clés série, séparés par des virgules (défaut : prizm,prism).",
    )
    parser.add_argument(
        "--rookie-keywords",
        default=None,
        help="Mots-clés rookie, séparés par des virgules (défaut : rookie,rc,2012,2012-13,12-13).",
    )
    parser.add_argument(
        "--exclude-keywords",
        default=None,
        help="Mots-clés à exclure (défaut : bucky).",
    )
    parser.add_argument(
        "--no-require-rookie",
        action="store_true",
        help="Ne pas exiger un indice rookie/RC/2012 dans le titre (garde seulement nom + Prizm).",
    )
    parser.add_argument(
        "--show-all",
        action="store_true",
        help="Afficher aussi les annonces déjà vues (pas seulement les nouvelles).",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Logs plus détaillés.")
    args = parser.parse_args(argv)
    args.queries = args.query_args or list(DEFAULT_QUERIES)
    args.name_keywords = split_keywords(args.name_keywords, DEFAULT_NAME_KEYWORDS)
    args.set_keywords = split_keywords(args.set_keywords, DEFAULT_SET_KEYWORDS)
    args.rookie_keywords = split_keywords(args.rookie_keywords, DEFAULT_ROOKIE_KEYWORDS)
    args.exclude_keywords = split_keywords(args.exclude_keywords, DEFAULT_EXCLUDE_KEYWORDS)
    args.require_rookie = not args.no_require_rookie
    if args.interval <= 0:
        parser.error("--interval doit être strictement positif.")
    if args.per_page <= 0 or args.max_pages <= 0:
        parser.error("--per-page et --max-pages doivent être strictement positifs.")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    client = VintedClient(args.domain, locale=args.locale)
    store = SeenStore(args.seen_file)
    logger.info(
        "Surveillance %s (%s) — requêtes : %s — intervalle : %s min — ids déjà vus : %s",
        client.www_host,
        client.locale,
        ", ".join(args.queries),
        args.interval,
        len(store.items),
    )

    if args.once:
        return run_once(client, store, args)

    exit_code = 0
    while True:
        try:
            code = run_once(client, store, args)
            if code != 0:
                exit_code = code
                logger.warning("Passage en échec, on continue après l'intervalle.")
        except KeyboardInterrupt:
            print("\nArrêt demandé.", flush=True)
            return exit_code
        except Exception:
            logger.exception("Erreur inattendue pendant un passage — on continue.")
            exit_code = 1
        try:
            time.sleep(args.interval * 60)
        except KeyboardInterrupt:
            print("\nArrêt demandé.", flush=True)
            return exit_code


if __name__ == "__main__":
    sys.exit(main())
