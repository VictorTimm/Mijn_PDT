"""Fill holding classification from a local cache, then outside sources.

Transactions still only get ticker aliases here. Holdings gain sector,
industry, country, exchange, security_type, and currency.

Lookup order for a symbol that is not already in ``data/instruments.json``:

1. finance-database (the ``financedatabase`` package)
2. Yahoo Finance via ``yfinance``
3. :data:`OVERRIDES` for coins and anything both sources leave blank

A symbol already present in the cache is not looked up again. Overrides still
fill fields the cache left empty, so a newly added coin does not need a refetch.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pandas as pd

from processing.models import FIAT, HOLDING_COLUMNS, TRANSACTION_COLUMNS
from processing.normalize import DATA_DIR

ALIASES = {
    "XBT": "BTC",
    "MIOTA": "IOTA",
    "ETH2": "ETH",
}

PROFILE_FIELDS = ("sector", "industry", "country", "exchange", "security_type", "currency")
CACHE_PATH = DATA_DIR / "instruments.json"
OVERRIDES_PATH = DATA_DIR.parent / "overrides.json"
PRICE_CACHE_PATH = DATA_DIR / "prices.json"
ETF_TARGETS_PATH = DATA_DIR / "etf_composition_targets.json"
ETF_COMPOSITION_PATH = DATA_DIR / "etf_composition.json"
PRICE_CACHE_TTL = timedelta(hours=12)
MAPPING_CACHE_TTL = timedelta(days=7)
MAPPING_FAILURE_LIMIT = 3
PREFERRED_EXCHANGES = ("EAM", "AMS", "AEX", "XAMS", "XETRA", "XFRA", "FRA", "GER")
CRYPTO_BROKERS = frozenset({"bitvavo", "ledger"})
BITVAVO_TICKER_URL = "https://api.bitvavo.com/v2/ticker/price"
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
FORCED_UNPRICED_SYMBOLS = frozenset({"LUNA", "BLOSK"})
CRYPTO_PRICE_ALIASES = {
    "FTM": ("SONIC", "S"),
    "S": ("SONIC",),
}

# Last resort for coins and for instruments both databases miss.
# Values are the six profile fields. An empty string leaves the book value in place.
OVERRIDES: dict[str, dict[str, str]] = {
    "BTC": {"sector": "Crypto", "industry": "Bitcoin", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "ETH": {"sector": "Crypto", "industry": "Ethereum", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "ADA": {"sector": "Crypto", "industry": "Cardano", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "SOL": {"sector": "Crypto", "industry": "Solana", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "XRP": {"sector": "Crypto", "industry": "XRP", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "DOGE": {"sector": "Crypto", "industry": "Dogecoin", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "DOT": {"sector": "Crypto", "industry": "Polkadot", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "AVAX": {"sector": "Crypto", "industry": "Avalanche", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "LINK": {"sector": "Crypto", "industry": "Chainlink", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "ATOM": {"sector": "Crypto", "industry": "Cosmos", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "LTC": {"sector": "Crypto", "industry": "Litecoin", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "BCH": {"sector": "Crypto", "industry": "Bitcoin Cash", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "XLM": {"sector": "Crypto", "industry": "Stellar", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "UNI": {"sector": "Crypto", "industry": "Uniswap", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "AAVE": {"sector": "Crypto", "industry": "Aave", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "NEAR": {"sector": "Crypto", "industry": "NEAR", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "ICP": {"sector": "Crypto", "industry": "Internet Computer", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "MATIC": {"sector": "Crypto", "industry": "Polygon", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "POL": {"sector": "Crypto", "industry": "Polygon", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "IOTA": {"sector": "Crypto", "industry": "IOTA", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "ALGO": {"sector": "Crypto", "industry": "Algorand", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "AXS": {"sector": "Crypto", "industry": "Axie Infinity", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "CHZ": {"sector": "Crypto", "industry": "Chiliz", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "ENJ": {"sector": "Crypto", "industry": "Enjin", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "HBAR": {"sector": "Crypto", "industry": "Hedera", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "QNT": {"sector": "Crypto", "industry": "Quant", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "USDC": {"sector": "Crypto", "industry": "USDC", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": "USD"},
    "FTM": {"sector": "Crypto", "industry": "Sonic", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "S": {"sector": "Crypto", "industry": "Sonic", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "SONIC": {"sector": "Crypto", "industry": "Sonic", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "LUNA": {"sector": "Crypto", "industry": "Luna", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
    "BLOSK": {"sector": "Crypto", "industry": "BLOSK", "country": "Global", "exchange": "", "security_type": "Crypto", "currency": ""},
}

_CRYPTO_FALLBACK = {
    "sector": "Crypto",
    "industry": "Cryptocurrency",
    "country": "Global",
    "exchange": "",
    "security_type": "Crypto",
    "currency": "",
}

_YAHOO_TYPES = {
    "EQUITY": "equity",
    "ETF": "etf",
    "MUTUALFUND": "fund",
    "INDEX": "index",
    "CRYPTOCURRENCY": "Crypto",
}

_databases: dict[str, object] = {}
_etf_ticker_hints_cache: dict[str, str] | None = None


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].replace(ALIASES)
    frame["name"] = frame["name"].fillna("").astype(str).str.strip()
    missing_name = frame["name"].eq("")
    frame.loc[missing_name, "name"] = frame.loc[missing_name, "symbol"]
    return frame.loc[:, list(TRANSACTION_COLUMNS)]


def apply_user_overrides(
    holdings: pd.DataFrame,
    path: Path | None = None,
) -> tuple[pd.DataFrame, str | None]:
    """Replace sector, country, and the other profile fields from overrides.json.

    Keys are symbols or ISINs. A value only replaces a field when it is filled in.
    """
    frame = holdings.copy()
    if frame.empty:
        return frame, None
    overrides, error = _read_user_overrides(path or OVERRIDES_PATH)
    if error or not overrides:
        return frame, error
    for index, row in frame.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        isin = str(row.get("isin") or "").strip().upper()
        chosen = {}
        if symbol in overrides:
            chosen.update(overrides[symbol])
        if isin in overrides:
            chosen.update(overrides[isin])
        for field, value in chosen.items():
            if field in PROFILE_FIELDS and value:
                frame.at[index, field] = value
    return frame, None


def _read_user_overrides(path: Path) -> tuple[dict[str, dict[str, str]], str | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}, "overrides.json is not valid JSON. Use a symbol, then sector and country, and save the file again."
    except OSError:
        return {}, "overrides.json could not be opened."
    if not isinstance(payload, dict):
        return {}, 'overrides.json should list symbols, for example {"VWRL": {"sector": "Diversified", "country": "Global"}}.'
    overrides: dict[str, dict[str, str]] = {}
    for key, profile in payload.items():
        if not isinstance(profile, dict):
            continue
        cleaned = {field: _text(profile.get(field)) for field in PROFILE_FIELDS if _text(profile.get(field))}
        if cleaned:
            overrides[str(key).strip().upper()] = cleaned
    return overrides, None


def latest_prices(df: pd.DataFrame) -> dict[str, float]:
    values, _ = _latest_prices_with_dates(df)
    return values


def _latest_prices_with_dates(df: pd.DataFrame) -> tuple[dict[str, float], dict[str, str]]:
    if df.empty or "price" not in df.columns:
        return {}, {}
    quoted = df["original_currency"].fillna("").eq("EUR")
    priced = df.loc[
        df["price"].notna() & df["price"].gt(0) & quoted & ~df["symbol"].isin(FIAT)
    ].sort_values("date")
    if priced.empty:
        return {}, {}
    isin = priced["isin"].fillna("")
    key = isin.where(isin.ne(""), priced["symbol"])
    latest = priced.assign(_key=key).groupby("_key", sort=False).tail(1)
    values = dict(zip(latest["_key"], latest["price"].astype(float)))
    stamps: dict[str, str] = {}
    for row in latest[["_key", "date"]].itertuples(index=False, name=None):
        key, stamp = row
        if pd.notna(stamp):
            stamps[str(key)] = pd.Timestamp(stamp).isoformat()
    return values, stamps


def live_prices(
    transactions: pd.DataFrame,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, float]:
    """Return live-or-cached EUR prices keyed by ISIN (or symbol fallback)."""
    hints, _, _ = _price_hints_bundle(transactions, cache_path=cache_path, now=now)
    return hints


def price_hints_with_sources(
    transactions: pd.DataFrame,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, float], dict[str, str]]:
    hints, sources, _ = _price_hints_bundle(transactions, cache_path=cache_path, now=now)
    return hints, sources


def price_hints_with_details(
    transactions: pd.DataFrame,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, float], dict[str, str], dict[str, dict[str, str]]]:
    return _price_hints_bundle(transactions, cache_path=cache_path, now=now)


def _price_hints_bundle(
    transactions: pd.DataFrame,
    cache_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, float], dict[str, str], dict[str, dict[str, str]]]:
    """Build deterministic EUR hints and track the source of each hint.

    Source order per security key, on every call:
    1) live quote: Bitvavo for crypto, Yahoo Finance for everything else
    2) the last saved EUR quote, only when that live request fails
    3) latest EUR trade from CSV
    """
    if transactions is None or transactions.empty:
        return {}, {}, {}
    stamp = now or datetime.now(timezone.utc)
    cache_file = cache_path or PRICE_CACHE_PATH
    cache = _load_price_cache(cache_file)
    csv_prices, csv_dates = _latest_prices_with_dates(transactions)
    hints: dict[str, float] = {}
    sources: dict[str, str] = {}
    details: dict[str, dict[str, str]] = {}
    dirty = False
    wanted = _wanted_keys(transactions)
    crypto_prices = _bitvavo_eur_prices() if any(item["crypto"] for item in wanted.values()) else {}
    for key, item in wanted.items():
        query = item["query"]
        ticker_hint = item.get("ticker_hint", "")
        force_unpriced = bool(item.get("force_unpriced"))
        if force_unpriced:
            sources[key] = "unpriced"
            details[key] = {"source": "unpriced", "as_of": ""}
            continue
        entry = cache.get(key) if isinstance(cache.get(key), dict) else {}
        if not entry:
            entry = {
                "isin_or_key": key,
                "ticker": str(ticker_hint or "").strip().upper(),
                "exchange": "",
                "quote_currency": "",
                "price_eur": None,
                "as_of": "",
                "source": "",
                "confidence": "",
                "mapped_at": "",
                "failure_count": 0,
            }
            cache[key] = entry
            dirty = True
        elif ticker_hint and not _text(entry.get("ticker")):
            entry["ticker"] = str(ticker_hint).strip().upper()
            dirty = True

        if item["crypto"]:
            direct = _crypto_live_entry(query, crypto_prices, stamp)
            if direct is not None:
                entry.update(direct)
                entry["isin_or_key"] = key
                entry["failure_count"] = 0
                live = {"price_eur": float(direct["price_eur"])}
            else:
                live = None
        else:
            live = _resolve_live_entry(query, key, entry, stamp, str(ticker_hint or ""))
        if live is None and not item["crypto"]:
            quoted = _justetf_eur_quote(key, stamp)
            if quoted is not None:
                entry.update(quoted)
                entry["isin_or_key"] = key
                entry["failure_count"] = 0
                live = {"price_eur": float(quoted["price_eur"])}
        if live is not None:
            hints[key] = live["price_eur"]
            sources[key] = "live"
            details[key] = {"source": "live", "as_of": _text(entry.get("as_of"))}
            dirty = True
            continue

        cached_price = _num(entry.get("price_eur"))
        if cached_price is not None and cached_price > 0:
            hints[key] = cached_price
            sources[key] = "cache"
            details[key] = {"source": "cache", "as_of": _text(entry.get("as_of"))}
            continue

        csv = _num(csv_prices.get(key))
        if csv is not None and csv > 0:
            hints[key] = csv
            sources[key] = "csv"
            details[key] = {"source": "csv", "as_of": _text(csv_dates.get(key))}
            continue
        details[key] = {"source": "unpriced", "as_of": ""}

    if dirty:
        _save_price_cache(cache_file, cache)
    return hints, sources, details


def enrich_holdings(holdings: pd.DataFrame, cache_path: Path | None = None) -> pd.DataFrame:
    """Add classification columns. Network lookups run only for uncached symbols."""
    frame = holdings.copy()
    if frame.empty:
        return frame
    for column in HOLDING_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    path = cache_path or CACHE_PATH
    cache = _load_cache(path)
    dirty = False
    for index, row in frame.iterrows():
        symbol = str(row.get("symbol") or "").strip().upper()
        isin = str(row.get("isin") or "").strip().upper()
        key = isin or symbol
        if not key or key in FIAT:
            continue
        cached = cache.get(key) if isinstance(cache.get(key), dict) else _blank()
        if key in cache and _blank_profile(cached):
            cache.pop(key, None)
            cached = _blank()
            dirty = True
        if key not in cache:
            looked_up = _lookup(symbol, isin)
            if _classified(looked_up):
                if key not in cache or cache.get(key) != looked_up:
                    cache[key] = looked_up
                    dirty = True
                cached = looked_up
            else:
                cached = looked_up
        profile = _fill(_blank(), _fill(cached, _override(symbol, isin, str(row.get("security_type") or ""))))
        for field in PROFILE_FIELDS:
            frame.at[index, field] = _prefer(field, row.get(field), profile.get(field, ""))
    if dirty:
        _save_cache(path, cache)
    return frame.loc[:, list(HOLDING_COLUMNS)]


def _lookup(symbol: str, isin: str) -> dict[str, str]:
    profile = _blank()
    profile = _fill(profile, _from_finance_database(symbol, isin))
    if not _classified(profile):
        profile = _fill(profile, _from_yfinance(symbol, isin))
    return profile


def _override(symbol: str, isin: str, security_type: str) -> dict[str, str]:
    for key in (symbol, isin):
        if key in OVERRIDES:
            return OVERRIDES[key]
    hint = _etf_ticker_hints().get(isin or symbol, "")
    if hint:
        return {
            "sector": "",
            "industry": "",
            "country": "",
            "exchange": _exchange_from_ticker(hint),
            "security_type": "etf",
            "currency": "",
        }
    if security_type == "Crypto":
        return _CRYPTO_FALLBACK
    return _blank()


def _from_finance_database(symbol: str, isin: str) -> dict[str, str]:
    try:
        import financedatabase as fd
    except Exception:
        return _blank()
    catalogues = (
        ("equity", fd.Equities),
        ("etf", fd.ETFs),
        ("fund", fd.Funds),
        ("Crypto", fd.Cryptos),
    )
    for security_type, factory in catalogues:
        database = _database(security_type, factory)
        if database is None:
            continue
        row = _match_row(database, symbol, isin)
        if row is None:
            continue
        profile = _profile_from_mapping(row, security_type)
        if _classified(profile) or security_type == "Crypto":
            return profile
    return _blank()


def _database(security_type: str, factory):
    if security_type in _databases:
        return _databases[security_type]
    try:
        database = factory()
    except Exception:
        database = None
    _databases[security_type] = database
    return database


def _match_row(database, symbol: str, isin: str):
    try:
        data = database.select()
    except Exception:
        return None
    if data is None or len(data) == 0:
        return None
    wanted = {identifier for identifier in _identifiers(symbol, isin) if _ISIN.match(identifier)}
    if wanted and "isin" in getattr(data, "columns", []):
        codes = data["isin"].astype(str).str.upper()
        hits = data.loc[codes.isin(wanted)]
        if not hits.empty:
            return hits.iloc[0]
    for identifier in _identifiers(symbol, isin):
        if identifier in data.index:
            row = data.loc[identifier]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            return row
    return None


def _from_yfinance(symbol: str, isin: str) -> dict[str, str]:
    try:
        import yfinance as yf
    except Exception:
        return _blank()
    for identifier in _yahoo_candidates(yf, symbol, isin):
        info = _yahoo_info(yf, identifier)
        profile = _profile_from_yahoo(info)
        if _classified(profile):
            return profile
    return _blank()


def _yahoo_candidates(yf, symbol: str, isin: str) -> list[str]:
    candidates: list[str] = []
    for identifier in _identifiers(symbol, isin):
        if _ISIN.match(identifier):
            candidates.extend(_yahoo_search(yf, identifier))
        elif identifier not in candidates:
            candidates.append(identifier)
    for identifier in _identifiers(symbol, isin):
        if identifier not in candidates:
            candidates.append(identifier)
    return candidates


def _yahoo_search(yf, query: str) -> list[str]:
    search = getattr(yf, "Search", None)
    if search is None:
        return []
    try:
        result = search(query, max_results=5)
        quotes = getattr(result, "quotes", None) or []
    except Exception:
        return []
    symbols = []
    for quote in quotes:
        ticker = str(quote.get("symbol") or "").strip().upper()
        if ticker and ticker not in symbols:
            symbols.append(ticker)
    return symbols


def _yahoo_info(yf, identifier: str) -> dict:
    try:
        info = yf.Ticker(identifier).info
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _profile_from_mapping(row, security_type: str) -> dict[str, str]:
    def cell(*names: str) -> str:
        for name in names:
            if name in row:
                text = _text(row[name])
                if text:
                    return text
        return ""

    return {
        "sector": cell("sector", "category_group"),
        "industry": cell("industry", "industry_group", "category"),
        "country": cell("country"),
        "exchange": cell("exchange", "market"),
        "security_type": security_type,
        "currency": cell("currency"),
    }


def _profile_from_yahoo(info: dict) -> dict[str, str]:
    quote_type = _YAHOO_TYPES.get(str(info.get("quoteType") or "").upper(), "")
    category = _text(info.get("category"))
    return {
        "sector": _text(info.get("sector")) or category,
        "industry": _text(info.get("industry")) or category,
        "country": _text(info.get("country")),
        "exchange": _text(info.get("fullExchangeName") or info.get("exchange")),
        "security_type": quote_type,
        "currency": _text(info.get("currency")),
    }


def _identifiers(symbol: str, isin: str) -> list[str]:
    found = []
    for value in (isin, symbol):
        text = str(value or "").strip().upper()
        if text and text not in found and text not in FIAT:
            found.append(text)
    return found


def _classified(profile: dict[str, str]) -> bool:
    return any(profile.get(field) for field in ("sector", "industry", "country", "exchange"))


def _blank_profile(profile: dict[str, str]) -> bool:
    return not any(_text(profile.get(field)) for field in PROFILE_FIELDS)


def _prefer(field: str, current, incoming: str) -> str:
    present = _text(current)
    incoming = _text(incoming)
    if not incoming:
        return present
    if not present:
        return incoming
    if field == "security_type" and present == "equity" and incoming in {"etf", "fund"}:
        return incoming
    return present


def _fill(base: dict[str, str], extra: dict[str, str] | None) -> dict[str, str]:
    merged = dict(base)
    if not extra:
        return merged
    for field in PROFILE_FIELDS:
        if not merged.get(field) and extra.get(field):
            merged[field] = extra[field]
    return merged


def _blank() -> dict[str, str]:
    return {field: "" for field in PROFILE_FIELDS}


def _text(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null", "<na>"}:
        return ""
    return text


def _load_cache(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    cache = {}
    for key, profile in payload.items():
        if isinstance(profile, dict):
            cache[str(key).upper()] = _fill(_blank(), {field: _text(profile.get(field)) for field in PROFILE_FIELDS})
    return cache


def _save_cache(path: Path, cache: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _crypto_live_entry(symbol: str, prices: dict[str, float], now: datetime) -> dict[str, float] | None:
    chosen_symbol = symbol
    price = prices.get(symbol)
    if price is None or price <= 0:
        for alias in CRYPTO_PRICE_ALIASES.get(symbol, ()):
            alias_price = prices.get(alias)
            if alias_price is not None and alias_price > 0:
                chosen_symbol = alias
                price = alias_price
                break
    if price is None or price <= 0:
        return None
    return {
        "ticker": f"{chosen_symbol}-EUR",
        "exchange": "Bitvavo",
        "quote_currency": "EUR",
        "price_eur": float(price),
        "as_of": now.isoformat(),
        "source": "bitvavo",
        "confidence": "crypto",
        "mapped_at": now.isoformat(),
    }


def _justetf_eur_quote(isin: str, now: datetime) -> dict[str, object] | None:
    """Euro quote from justETF when Yahoo Finance cannot be reached."""
    if not _ISIN.match(str(isin or "").strip().upper()):
        return None
    url = f"https://www.justetf.com/api/etfs/{isin}/quote?locale=en&currency=EUR"
    try:
        from urllib.request import Request, urlopen

        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    quote = payload.get("latestQuote") if isinstance(payload, dict) else None
    price = _num(quote.get("raw")) if isinstance(quote, dict) else None
    if price is None or price <= 0:
        return None
    return {
        "quote_currency": "EUR",
        "price_eur": float(price),
        "as_of": _text(payload.get("latestQuoteDate")) or now.date().isoformat(),
        "source": "justetf",
        "confidence": "eur",
        "mapped_at": now.isoformat(),
    }


def _bitvavo_eur_prices() -> dict[str, float]:
    """Public Bitvavo euro tickers. No API key. Empty when the call fails."""
    try:
        from urllib.request import Request, urlopen

        request = Request(BITVAVO_TICKER_URL, headers={"Accept": "application/json", "User-Agent": "MijnPDT"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, list):
        return {}
    prices: dict[str, float] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        market = _text(row.get("market")).upper()
        if not market.endswith("-EUR"):
            continue
        price = _num(row.get("price"))
        if price is None or price <= 0:
            continue
        prices[market[: -len("-EUR")]] = price
    return prices


def _resolve_live_entry(query: str, key: str, entry: dict, now: datetime, preferred_ticker: str = "") -> dict[str, float] | None:
    direct = _fetch_live_with_mapping(query, entry, now, preferred_ticker)
    if direct is not None:
        entry.update(direct)
        entry["isin_or_key"] = key
        entry["failure_count"] = 0
        return {"price_eur": float(direct["price_eur"])}
    failures = int(_num(entry.get("failure_count")) or 0) + 1
    entry["failure_count"] = failures
    if failures >= MAPPING_FAILURE_LIMIT and not _num(entry.get("price_eur")):
        entry["ticker"] = ""
        entry["mapped_at"] = ""
        entry["failure_count"] = 0
    return None


def _fetch_live_with_mapping(query: str, entry: dict, now: datetime, preferred_ticker: str = "") -> dict[str, object] | None:
    try:
        import yfinance as yf
    except Exception:
        yf = None
    ticker_symbol = _text(entry.get("ticker")).upper()
    quote_currency = _text(entry.get("quote_currency")).upper()
    exchange = _text(entry.get("exchange"))
    confidence = _text(entry.get("confidence")) or "search"
    source = _text(entry.get("source")) or "search"
    mapped_at = _parse_timestamp(entry.get("mapped_at"))
    stale_mapping = mapped_at is None or now - mapped_at > MAPPING_CACHE_TTL
    previous_ticker = ticker_symbol
    previous_currency = quote_currency
    previous_exchange = exchange
    chosen_hint = _text(preferred_ticker).upper()
    if chosen_hint and (not ticker_symbol or stale_mapping):
        ticker_symbol = chosen_hint
        quote_currency = previous_currency
        exchange = _exchange_from_ticker(chosen_hint) or exchange
        confidence = "target"
        source = "target"
        mapped_at = now
        stale_mapping = False
    if not ticker_symbol or stale_mapping:
        selected = None
        if yf is not None:
            quotes = _yahoo_quotes(yf, query)
            selected = _pick_quote(quotes)
            if selected is None and not _ISIN.match(query):
                selected = {"symbol": query, "currency": "", "exchange": "", "confidence": "symbol", "source": "symbol"}
        if selected is not None:
            ticker_symbol = selected["symbol"]
            quote_currency = selected["currency"]
            exchange = selected["exchange"]
            confidence = selected["confidence"]
            source = selected["source"]
            mapped_at = now
        elif previous_ticker:
            ticker_symbol = previous_ticker
            quote_currency = previous_currency
            exchange = previous_exchange
        else:
            return None

    price = None
    ticker = None
    chart_currency = ""
    chart_as_of = ""
    if yf is not None:
        try:
            ticker = yf.Ticker(ticker_symbol)
            price = _yahoo_price(ticker)
        except Exception:
            price = None
    if price is None or price <= 0:
        chart = _yahoo_chart_quote(ticker_symbol)
        if chart is not None:
            price = float(chart["price"])
            chart_currency = str(chart["currency"])
            chart_as_of = str(chart["as_of"])
            source = "yahoo"
    if price is None or price <= 0:
        return None
    live_currency = chart_currency or (_yahoo_currency(ticker) if ticker is not None else "")
    currency = (live_currency or quote_currency).upper()
    if currency != "EUR" and _ISIN.match(query):
        euro = _justetf_eur_quote(query, now)
        if euro is not None:
            return {
                "ticker": ticker_symbol,
                "exchange": exchange,
                "quote_currency": "EUR",
                "price_eur": float(euro["price_eur"]),
                "as_of": _text(euro.get("as_of")) or now.isoformat(),
                "source": "justetf",
                "confidence": "eur",
                "mapped_at": (mapped_at or now).isoformat(),
            }
    if not currency:
        return None
    if currency == "EUR":
        price_eur = float(price)
    else:
        converted = _to_eur(yf, float(price), currency) if yf is not None else None
        if converted is None:
            converted = _chart_to_eur(float(price), currency)
        if converted is None:
            return None
        price_eur = converted
    return {
        "ticker": ticker_symbol,
        "exchange": exchange,
        "quote_currency": currency,
        "price_eur": price_eur,
        "as_of": chart_as_of or now.isoformat(),
        "source": source,
        "confidence": confidence,
        "mapped_at": (mapped_at or now).isoformat(),
    }


def _yahoo_chart_quote(symbol: str) -> dict[str, object] | None:
    """Last trade from Yahoo's public chart feed. No API key."""
    ticker = str(symbol or "").strip()
    if not ticker:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}?interval=1d&range=5d"
    try:
        from urllib.request import Request, urlopen

        request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        with urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    results = payload.get("chart", {}).get("result") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None
    meta = results[0].get("meta") if isinstance(results[0], dict) else None
    if not isinstance(meta, dict):
        return None
    price = _num(meta.get("regularMarketPrice"))
    currency = _text(meta.get("currency")).upper()
    if price is None or price <= 0 or not currency:
        return None
    if currency == "GBX":
        price = price / 100.0
        currency = "GBP"
    stamp = _num(meta.get("regularMarketTime"))
    if stamp is not None and stamp > 0:
        as_of = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    else:
        as_of = ""
    return {"price": float(price), "currency": currency, "as_of": as_of}


def _chart_to_eur(price: float, currency: str) -> float | None:
    code = str(currency or "").strip().upper()
    if code == "EUR":
        return float(price)
    if not code:
        return None
    direct = _yahoo_chart_quote(f"{code}EUR=X")
    if direct is not None and float(direct["price"]) > 0:
        return float(price) * float(direct["price"])
    inverse = _yahoo_chart_quote(f"EUR{code}=X")
    if inverse is not None and float(inverse["price"]) > 0:
        return float(price) / float(inverse["price"])
    return None


def _yahoo_quotes(yf, query: str) -> list[dict]:
    search = getattr(yf, "Search", None)
    if search is None:
        return []
    try:
        result = search(query, max_results=8)
    except Exception:
        return []
    quotes = getattr(result, "quotes", None)
    return quotes if isinstance(quotes, list) else []


def _pick_quote(quotes: list[dict]) -> dict[str, str] | None:
    if not quotes:
        return None
    ranked: list[tuple[int, dict[str, str]]] = []
    for quote in quotes:
        symbol = str(quote.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        quote_type = str(quote.get("quoteType") or "").upper()
        if quote_type == "CRYPTOCURRENCY":
            continue
        currency = str(quote.get("currency") or "").strip().upper()
        exchange = str(quote.get("exchange") or quote.get("fullExchangeName") or "").strip()
        market_price = _num(quote.get("regularMarketPrice"))
        score = 0
        if quote_type in {"ETF", "EQUITY"}:
            score += 40
        if currency == "EUR":
            score += 25
        if any(token in exchange.upper() for token in PREFERRED_EXCHANGES):
            score += 10
        if market_price is not None and market_price > 0:
            score += 5
        confidence = "high" if currency == "EUR" and quote_type in {"ETF", "EQUITY"} else "search"
        ranked.append(
            (
                score,
                {
                    "symbol": symbol,
                    "currency": currency,
                    "exchange": exchange,
                    "confidence": confidence,
                    "source": "search",
                },
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _yahoo_price(ticker) -> float | None:
    try:
        fast = ticker.fast_info
    except Exception:
        fast = None
    if fast:
        value = _num(_mapping_get(fast, "last_price"))
        if value is not None and value > 0:
            return value
    try:
        info = ticker.info
    except Exception:
        info = None
    if isinstance(info, dict):
        for field in ("regularMarketPrice", "currentPrice"):
            value = _num(info.get(field))
            if value is not None and value > 0:
                return value
    return None


def _yahoo_currency(ticker) -> str:
    try:
        fast = ticker.fast_info
    except Exception:
        fast = None
    if fast:
        code = _text(_mapping_get(fast, "currency")).upper()
        if code:
            return code
    try:
        info = ticker.info
    except Exception:
        info = None
    if isinstance(info, dict):
        return _text(info.get("currency")).upper()
    return ""


def _to_eur(yf, price: float, currency: str) -> float | None:
    pair = f"{currency}EUR=X"
    fx = _yahoo_price(yf.Ticker(pair))
    if fx is None or fx <= 0:
        return None
    return float(price) * float(fx)


def _is_fresh(entry: dict, now: datetime) -> bool:
    stamp = _parse_timestamp(entry.get("as_of"))
    if stamp is None:
        return False
    return now - stamp <= PRICE_CACHE_TTL


def _parse_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_price_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = _json_load(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    cleaned: dict[str, dict] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            row = dict(value)
            row["isin_or_key"] = _text(row.get("isin_or_key")) or str(key).strip().upper()
            row["ticker"] = _text(row.get("ticker") or row.get("symbol")).upper()
            row["exchange"] = _text(row.get("exchange"))
            row["quote_currency"] = _text(row.get("quote_currency") or row.get("currency")).upper()
            row["source"] = _text(row.get("source"))
            row["confidence"] = _text(row.get("confidence"))
            row["mapped_at"] = _text(row.get("mapped_at"))
            row["as_of"] = _text(row.get("as_of"))
            price = _num(row.get("price_eur"))
            row["price_eur"] = float(price) if price is not None and price > 0 else None
            row["failure_count"] = int(_num(row.get("failure_count")) or 0)
            cleaned[str(key).strip().upper()] = row
    return cleaned


def _save_price_cache(path: Path, cache: dict[str, dict]) -> None:
    cleaned: dict[str, dict] = {}
    for key, value in cache.items():
        if not isinstance(value, dict):
            continue
        row = dict(value)
        price = _num(row.get("price_eur"))
        row["price_eur"] = float(price) if price is not None and price > 0 else None
        cleaned[str(key).strip().upper()] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cleaned, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number


def _mapping_get(mapping, key: str):
    if isinstance(mapping, dict):
        return mapping.get(key)
    try:
        return getattr(mapping, key, None)
    except Exception:
        return None


def _wanted_keys(transactions: pd.DataFrame) -> dict[str, dict[str, object]]:
    hints = _etf_ticker_hints()
    wanted: dict[str, dict[str, object]] = {}
    for row in transactions.itertuples(index=False):
        symbol = str(getattr(row, "symbol", "") or "").strip().upper()
        isin = str(getattr(row, "isin", "") or "").strip().upper()
        broker = str(getattr(row, "broker", "") or "").strip().lower()
        key = isin or symbol
        query = isin or symbol
        if not key or key in FIAT or symbol in FIAT:
            continue
        crypto = symbol in OVERRIDES or (broker in CRYPTO_BROKERS and not isin)
        force_unpriced = symbol in FORCED_UNPRICED_SYMBOLS
        ticker_hint = hints.get(key, "")
        if key not in wanted:
            wanted[key] = {
                "query": query,
                "crypto": crypto,
                "symbol": symbol,
                "ticker_hint": ticker_hint,
                "force_unpriced": force_unpriced,
            }
        elif crypto:
            wanted[key]["crypto"] = True
        if force_unpriced:
            wanted[key]["force_unpriced"] = True
    return wanted


def _json_load(text: str):
    return json.loads(text, parse_constant=lambda _: None)


def _etf_ticker_hints() -> dict[str, str]:
    global _etf_ticker_hints_cache
    if _etf_ticker_hints_cache is not None:
        return _etf_ticker_hints_cache
    hints: dict[str, str] = {}
    for path in (ETF_TARGETS_PATH, ETF_COMPOSITION_PATH):
        if not path.exists():
            continue
        try:
            payload = _json_load(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            isin = str(key or "").strip().upper()
            ticker = _text(value.get("ticker")).upper()
            if isin and ticker and isin not in hints:
                hints[isin] = ticker
            if ticker and ticker not in hints:
                hints[ticker] = ticker
    _etf_ticker_hints_cache = hints
    return hints


def _exchange_from_ticker(ticker: str) -> str:
    code = str(ticker or "").strip().upper()
    if code.endswith(".AS"):
        return "Euronext Amsterdam"
    if code.endswith(".DE"):
        return "Xetra"
    if code.endswith(".L"):
        return "London Stock Exchange"
    return ""
