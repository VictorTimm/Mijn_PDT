"""ETF look-through helpers for allocation charts."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from processing.normalize import DATA_DIR

COMPOSITION_PATH = DATA_DIR / "etf_composition.json"

_SECTOR_ALIASES = {
    "technology": "Information technology",
    "information tech": "Information technology",
    "information technology": "Information technology",
    "it": "Information technology",
    "consumer discretionary": "Consumer discretionary",
    "consumer cyclical": "Consumer discretionary",
    "consumer cyclicals": "Consumer discretionary",
    "consumer non cyclical": "Consumer staples",
    "consumer defensive": "Consumer staples",
    "consumer non-cyclicals": "Consumer staples",
    "consumer staples": "Consumer staples",
    "communication services": "Communication services",
    "telecommunication services": "Communication services",
    "telecommunications": "Communication services",
    "health care": "Health care",
    "healthcare": "Health care",
    "realestate": "Real estate",
    "real estate": "Real estate",
    "utilities": "Utilities",
    "industrials": "Industrials",
    "industrials and materials": "Industrials",
    "non-energy materials": "Materials",
    "telecommunication": "Communication services",
    "financial": "Financials",
    "financial services": "Financials",
    "financials": "Financials",
    "finance": "Financials",
    "energy": "Energy",
    "basic materials": "Materials",
    "materials": "Materials",
    "other": "Rest of fund",
    "miscellaneous": "Rest of fund",
    "rest of fund": "Rest of fund",
}

_COUNTRY_ALIASES = {
    "united states": "United States",
    "usa": "United States",
    "us": "United States",
    "u.s.": "United States",
    "uk": "United Kingdom",
    "u.k.": "United Kingdom",
    "gb": "United Kingdom",
    "gbr": "United Kingdom",
    "great britain": "United Kingdom",
    "britain": "United Kingdom",
    "england": "United Kingdom",
    "south korea": "South Korea",
    "korea, republic of": "South Korea",
    "republic of korea": "South Korea",
    "other": "Rest of fund",
    "rest of fund": "Rest of fund",
}

_COUNTRY_CURRENCY = {
    "United States": "USD",
    "United Kingdom": "GBP",
    "Japan": "JPY",
    "Canada": "CAD",
    "Switzerland": "CHF",
    "China": "CNY",
    "Taiwan": "TWD",
    "India": "INR",
    "Luxembourg": "EUR",
    "Greece": "EUR",
    "Slovakia": "EUR",
    "Slovenia": "EUR",
    "Estonia": "EUR",
    "Latvia": "EUR",
    "Lithuania": "EUR",
    "Cyprus": "EUR",
    "Malta": "EUR",
    "Croatia": "EUR",
    "Germany": "EUR",
    "France": "EUR",
    "Netherlands": "EUR",
    "Ireland": "EUR",
    "Italy": "EUR",
    "Finland": "EUR",
    "Spain": "EUR",
    "Belgium": "EUR",
    "Austria": "EUR",
    "Portugal": "EUR",
    "Australia": "AUD",
    "New Zealand": "NZD",
    "Sweden": "SEK",
    "Norway": "NOK",
    "Denmark": "DKK",
    "Poland": "PLN",
    "Czech Republic": "CZK",
    "Hungary": "HUF",
    "Romania": "RON",
    "South Korea": "KRW",
    "Singapore": "SGD",
    "Hong Kong": "HKD",
    "Mexico": "MXN",
    "Brazil": "BRL",
    "South Africa": "ZAR",
    "Thailand": "THB",
    "Indonesia": "IDR",
    "Malaysia": "MYR",
    "Philippines": "PHP",
    "Puerto Rico": "USD",
    "Israel": "ILS",
    "Turkey": "TRY",
    "Other": "Other",
    "Rest of fund": "Other",
}

_USD_STABLECOINS = frozenset({"USDC", "USDT", "DAI", "USDP", "TUSD", "USDE"})


def load_etf_composition(path: Path | None = None) -> dict[str, dict[str, object]]:
    cache_path = path or COMPOSITION_PATH
    if not cache_path.exists():
        return {}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    loaded: dict[str, dict[str, object]] = {}
    for key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        sectors = _normalize_sectors(entry.get("sectors"))
        countries = _normalize_countries(entry.get("countries"))
        if not sectors and not countries:
            continue
        loaded[str(key).strip().upper()] = {
            "source": str(entry.get("source") or "").strip(),
            "as_of": str(entry.get("as_of") or "").strip(),
            "ticker": str(entry.get("ticker") or "").strip().upper(),
            "sectors": sectors,
            "countries": countries,
        }
    return loaded


def apply_lookthrough(
    holdings: pd.DataFrame,
    group_field: str,
    composition: dict[str, dict[str, object]],
) -> pd.DataFrame:
    """Split ETF/fund rows into group slices when look-through is available."""
    if holdings.empty or group_field not in {"sector", "country", "currency"}:
        return holdings
    bucket_name = "sectors" if group_field == "sector" else "countries"
    expanded: list[pd.Series] = []
    for row in holdings.itertuples(index=False):
        as_dict = row._asdict()
        security_type = str(as_dict.get("security_type") or "").strip().lower()
        if group_field == "country" and security_type == "crypto":
            as_dict["country"] = "Crypto"
        if group_field == "currency" and security_type == "crypto":
            symbol = str(as_dict.get("symbol") or "").strip().upper()
            as_dict["currency"] = "USD" if symbol in _USD_STABLECOINS else "Crypto"
        current_value = _num(as_dict.get("current_value"))
        if current_value is None or current_value <= 0:
            expanded.append(pd.Series(as_dict))
            continue
        if not composition:
            expanded.append(pd.Series(as_dict))
            continue
        isin = str(as_dict.get("isin") or "").strip().upper()
        symbol = str(as_dict.get("symbol") or "").strip().upper()
        found = composition.get(isin) or composition.get(symbol)
        buckets = found.get(bucket_name) if isinstance(found, dict) else None
        if not isinstance(buckets, dict) or not buckets:
            expanded.append(pd.Series(as_dict))
            continue
        currency_weights = _currency_weights_from_country(buckets) if group_field == "currency" else None
        chosen = currency_weights if isinstance(currency_weights, dict) and currency_weights else buckets
        target_field = "currency" if group_field == "currency" else group_field
        for label, weight in chosen.items():
            piece = dict(as_dict)
            piece[target_field] = label
            piece["current_value"] = float(current_value) * float(weight)
            expanded.append(pd.Series(piece))
    if not expanded:
        return holdings
    return pd.DataFrame(expanded, columns=holdings.columns)


def _normalize_sectors(value) -> dict[str, float]:
    return _normalize_weight_map(value, _sector_label)


def _normalize_countries(value) -> dict[str, float]:
    return _normalize_weight_map(value, _country_label)


def _normalize_weight_map(value, labeler) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    rows: dict[str, float] = {}
    for raw_name, raw_weight in value.items():
        weight = _num(raw_weight)
        if weight is None or weight <= 0:
            continue
        key = labeler(raw_name)
        if not key:
            continue
        rows[key] = rows.get(key, 0.0) + float(weight)
    if not rows:
        return {}
    scaled = _as_fractions(rows)
    total = sum(scaled.values())
    if total <= 0:
        return {}
    if total < 0.995:
        scaled["Rest of fund"] = scaled.get("Rest of fund", 0.0) + (1.0 - total)
        total = sum(scaled.values())
    if total > 1.005 or abs(total - 1.0) > 1e-9:
        scaled = {name: amount / total for name, amount in scaled.items()}
    return scaled


def _as_fractions(rows: dict[str, float]) -> dict[str, float]:
    if max(rows.values()) > 1.5:
        return {name: amount / 100.0 for name, amount in rows.items()}
    return dict(rows)


def _sector_label(value: object) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return ""
    if lowered in _SECTOR_ALIASES:
        return _SECTOR_ALIASES[lowered]
    return str(value).strip().title()


def _country_label(value: object) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return ""
    if lowered in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[lowered]
    return str(value).strip().title()


def _currency_weights_from_country(country_weights: dict[str, float]) -> dict[str, float]:
    rows: dict[str, float] = {}
    for country, weight in country_weights.items():
        code = _COUNTRY_CURRENCY.get(str(country).strip(), "Other")
        rows[code] = rows.get(code, 0.0) + float(weight)
    total = sum(rows.values())
    if total <= 0:
        return {}
    return {code: value / total for code, value in rows.items()}


def _num(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
