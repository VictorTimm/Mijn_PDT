"""Refresh ETF composition cache with per-ISIN source adapters."""

from __future__ import annotations

import http.cookiejar
import json
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, build_opener, HTTPCookieProcessor, urlopen

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processing.lookthrough import _normalize_countries, _normalize_sectors

TARGETS_PATH = ROOT / "data" / "etf_composition_targets.json"
CACHE_PATH = ROOT / "data" / "etf_composition.json"


def main() -> None:
    targets = _load_targets(TARGETS_PATH)
    if not targets:
        raise SystemExit("No targets found in data/etf_composition_targets.json")
    current = _load_json(CACHE_PATH)
    updated = 0
    kept = 0
    for isin, target in sorted(targets.items()):
        fetched = _fetch_from_sources(isin, target)
        if not fetched:
            kept += 1
            continue
        sectors = _normalize_sectors(fetched.get("sectors"))
        countries = _normalize_countries(fetched.get("countries"))
        if not _valid_breakdown(sectors, countries):
            kept += 1
            continue
        current[isin] = {
            "ticker": str(target.get("ticker") or "").strip().upper(),
            "source": str(fetched.get("source") or "").strip() or "unknown",
            "as_of": datetime.now(timezone.utc).date().isoformat(),
            "sectors": sectors,
            "countries": countries,
        }
        updated += 1
    CACHE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Updated {updated} ETF composition entries. Kept previous cache for {kept}.")


def _load_targets(path: Path) -> dict[str, dict[str, object]]:
    payload = _load_json(path)
    targets: dict[str, dict[str, object]] = {}
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        targets[str(key).strip().upper()] = {
            "ticker": str(value.get("ticker") or "").strip().upper(),
            "sources": value.get("sources") if isinstance(value.get("sources"), list) else [],
            "justetf_url": str(value.get("justetf_url") or "").strip(),
            "fund_url": str(value.get("fund_url") or "").strip(),
        }
    return targets


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fetch_from_sources(isin: str, target: dict[str, object]) -> dict[str, object]:
    sources = [str(item).strip() for item in target.get("sources") or [] if str(item).strip()]
    if not sources:
        sources = ["justetf"]
    for source in sources:
        if source == "globalx":
            found = _fetch_globalx(target)
        elif source in {"ishares_justetf", "justetf"}:
            found = _fetch_justetf(isin, target)
        else:
            found = {}
        if _valid_breakdown(found.get("sectors"), found.get("countries")):
            return found
    return {}


def _fetch_justetf(isin: str, target: dict[str, object]) -> dict[str, object]:
    url = str(target.get("justetf_url") or "").strip() or f"https://www.justetf.com/en/etf-profile.html?isin={isin}"
    html, opener = _fetch_justetf_page(url)
    if not html:
        return {}
    countries = _justetf_breakdown(html, opener, url, "countries")
    sectors = _justetf_breakdown(html, opener, url, "sectors")
    return {"source": "justetf_profile", "countries": countries, "sectors": sectors}


def _fetch_justetf_page(url: str):
    jar = http.cookiejar.CookieJar()
    opener = build_opener(HTTPCookieProcessor(jar))
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json"})
    try:
        with opener.open(request, timeout=15) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except Exception:
        return "", None
    return html, opener


def _justetf_breakdown(html: str, opener, page_url: str, kind: str) -> dict[str, float]:
    shown = dict(_extract_justetf_rows(html, kind))
    expanded_html = _fetch_justetf_expansion(html, opener, page_url, kind)
    expanded = dict(_extract_justetf_rows(expanded_html, kind)) if expanded_html else {}
    if len(expanded) > len(shown):
        return expanded
    return shown


def _fetch_justetf_expansion(html: str, opener, page_url: str, kind: str) -> str:
    path = _justetf_load_more_path(html, kind)
    if not path or opener is None:
        return ""
    ajax = "https://www.justetf.com" + path
    request = Request(
        ajax,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/xml",
            "Wicket-Ajax": "true",
            "Wicket-Ajax-BaseURL": page_url.split("justetf.com/", 1)[-1],
            "Referer": page_url,
        },
    )
    try:
        with opener.open(request, timeout=15) as response:
            return response.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _justetf_load_more_path(html: str, kind: str) -> str:
    token = "countries" if kind == "countries" else "sectors"
    match = re.search(rf'(/en/etf-profile\.html\?[^"\']*holdingsSection-{token}-loadMore[^"\']*)', html)
    if not match:
        return ""
    return unescape(match.group(1)).replace("&amp;", "&")


def _fetch_globalx(target: dict[str, object]) -> dict[str, object]:
    url = str(target.get("fund_url") or "").strip()
    if not url:
        return {}
    html = _fetch_text(url)
    if not html:
        return {}
    data = html.replace('\\"', '"')
    sectors = _extract_globalx_map(data, "Sector")
    countries = _extract_globalx_map(data, "Country")
    return {"source": "globalx_fund_page", "countries": countries, "sectors": sectors}


def _fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_justetf_rows(html: str, kind: str) -> list[tuple[str, float]]:
    token = "countries" if kind == "countries" else "sectors"
    pattern = (
        rf'data-testid="etf-holdings_{token}_row".*?'
        rf'etf-holdings_{token}_value_name">(?P<name>[^<]+)</td>.*?'
        rf'etf-holdings_{token}_value_percentage">(?P<pct>[0-9.,]+)%'
    )
    rows: list[tuple[str, float]] = []
    for match in re.finditer(pattern, html, flags=re.IGNORECASE | re.DOTALL):
        name = unescape(match.group("name")).strip()
        pct_text = match.group("pct").replace(",", ".")
        try:
            pct = float(pct_text) / 100.0
        except ValueError:
            continue
        rows.append((name, pct))
    return rows


def _extract_globalx_map(text: str, title: str) -> dict[str, float]:
    pattern = rf'"data":\{{(?P<data>[^\{{\}}]+)\}},"title":"{re.escape(title)}"'
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return {}
    raw = "{" + match.group("data") + "}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    out: dict[str, float] = {}
    for key, value in payload.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 1:
            number = number / 100.0
        if number > 0:
            out[str(key)] = number
    return out


def _valid_breakdown(sectors, countries) -> bool:
    sec = sectors if isinstance(sectors, dict) else {}
    ctry = countries if isinstance(countries, dict) else {}
    if not sec and not ctry:
        return False
    for bucket in (sec, ctry):
        if not bucket:
            continue
        total = sum(float(value) for value in bucket.values() if isinstance(value, (int, float)))
        if total <= 0:
            return False
    return True


if __name__ == "__main__":
    main()
