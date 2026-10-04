"""Read uploaded CSVs and turn the three importer tables into one book.

:func:`normalize` concatenates the DEGIRO, Bitvavo, and Ledger frames, replays
them at average cost, and returns transactions, holdings, and cash positions.
"""

from __future__ import annotations

import io
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from processing.models import FIAT, TRANSACTION_COLUMNS, empty_frame, make_row, records_frame

COLUMNS = TRANSACTION_COLUMNS
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EPS = 1e-8

_HEADER_KEYS = (
    "isin",
    "datum",
    "date",
    "quote currency",
    "operation amount",
    "omschrijving",
    "currency ticker",
)


@dataclass
class ParseResult:
    transactions: pd.DataFrame
    counts: list[tuple[str, str, int]]
    warnings: list[str]


def norm_header(name: object) -> str:
    text = str(name).strip().lower().replace("€", " eur ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_number(value: object) -> float:
    if value is None or isinstance(value, bool):
        return math.nan
    if isinstance(value, (int, float)):
        number = float(value)
        return math.nan if math.isnan(number) else number
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null", "-"}:
        return math.nan
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    text = text.replace("\u2212", "-").replace(" ", "")
    if text.endswith("-"):
        negative = True
        text = text[:-1]
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("-"):
        negative = True
        text = text[1:]
    text = re.sub(r"[^0-9,.]", "", text)
    if text in {"", ".", ","}:
        return math.nan
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return math.nan
    return -number if negative else number


def parse_datetime(date: object, time: object = None) -> pd.Timestamp:
    if date is None:
        return pd.NaT
    stamp = str(date).strip()
    if stamp == "" or stamp.lower() in {"nan", "none", "nat"}:
        return pd.NaT
    extra = "" if time is None else str(time).strip()
    if extra and extra.lower() not in {"nan", "none", "nat"}:
        stamp = f"{stamp} {extra}"
    dayfirst = not bool(re.match(r"^\d{4}[-/]", stamp))
    parsed = pd.to_datetime(stamp, dayfirst=dayfirst, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    timestamp = pd.Timestamp(parsed)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp


def records_to_frame(records: list[dict[str, object]]) -> pd.DataFrame:
    return records_frame(records, TRANSACTION_COLUMNS)


def cell_at(series: pd.Series | None, index: int) -> str:
    if series is None:
        return ""
    value = series.iloc[index]
    if pd.isna(value):
        return ""
    return str(value).strip()


def number_at(series: pd.Series | None, index: int) -> float:
    return parse_number(cell_at(series, index))


def find_column(
    df: pd.DataFrame,
    *aliases: str,
    with_currency: bool = False,
) -> pd.Series | None | tuple[pd.Series | None, pd.Series | None]:
    wanted = {norm_header(alias) for alias in aliases if norm_header(alias)}
    index = None
    normalized = [norm_header(column) for column in df.columns]
    for position, header in enumerate(normalized):
        if header in wanted:
            index = position
            break
    if index is None:
        for position, header in enumerate(normalized):
            if any(len(alias) >= 12 and header.startswith(alias) for alias in wanted):
                index = position
                break
    if index is None:
        return (None, None) if with_currency else None
    values = df.iloc[:, index]
    if not with_currency:
        return values
    currency = None
    if index + 1 < len(df.columns) and str(df.columns[index + 1]).strip() == "":
        candidate = df.iloc[:, index + 1]
        if _looks_like_currency(candidate):
            currency = candidate
    return values, currency


def classify(filename: str, columns: list[str], parsers: tuple[object, ...] | None = None):
    if parsers is None:
        from importers import PARSERS

        parsers = PARSERS
    hits = [parser for parser in parsers if parser.matches(filename, columns)]
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    lowered = filename.lower()
    for parser in hits:
        if parser.SOURCE in lowered:
            return parser
    return hits[0]


def parse_uploads(files: list[tuple[str, bytes]] | tuple[tuple[str, bytes], ...]) -> ParseResult:
    from importers import PARSERS

    frames: list[pd.DataFrame] = []
    counts: list[tuple[str, str, int]] = []
    warnings: list[str] = []
    for name, data in files:
        try:
            candidates = read_candidates(data)
        except ValueError as exc:
            warnings.append(_friendly_csv_error(name, str(exc)))
            continue
        except Exception:
            warnings.append(f"{name} could not be read. Save it again as a CSV and try once more.")
            continue
        table = None
        parser = None
        for candidate in sorted(candidates, key=lambda frame: frame.shape[1], reverse=True):
            parser = classify(name, list(candidate.columns), PARSERS)
            if parser is not None:
                table = candidate
                break
        if table is None or parser is None:
            warnings.append(
                f"{name} is not a DEGIRO, Bitvavo, or Ledger export. "
                "Use a DEGIRO account or transactions file, a Bitvavo transaction history, or a Ledger operations file."
            )
            continue
        try:
            parsed = parser.parse(table)
        except Exception:
            warnings.append(f"{name} looks damaged or incomplete, so its rows were skipped.")
            continue
        warning = parsed.attrs.get("warning")
        if warning:
            warnings.append(f"{name}: {warning}")
        if parsed.empty:
            label = {"degiro": "DEGIRO", "bitvavo": "Bitvavo", "ledger": "Ledger"}.get(parser.SOURCE, "broker")
            warnings.append(
                f"{name} looks like a {label} file, but none of the rows could be used. "
                "Check that it is the full export, not a summary."
            )
            continue
        counts.append((name, parser.SOURCE, int(len(parsed))))
        frames.append(parsed)
    if frames:
        transactions = finalize(pd.concat(frames, ignore_index=True))
    else:
        transactions = finalize(empty_frame(TRANSACTION_COLUMNS))
    from processing.enrich import enrich

    return ParseResult(enrich(transactions), counts, warnings)


def _friendly_csv_error(name: str, detail: str) -> str:
    if detail == "file is empty":
        return f"{name} is empty. Export the file from the broker and upload it again."
    if detail == "no table found":
        return f"{name} has no table. Save the broker export as a CSV and try again."
    return f"{name} could not be read. Save it again as a CSV from DEGIRO, Bitvavo, or Ledger."


def normalize(
    degiro: pd.DataFrame | None = None,
    bitvavo: pd.DataFrame | None = None,
    ledger: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Join the three importer tables and split holdings from cash.

    Buys add quantity and cost. Sells and transfers out reduce both at the
    current average. The cash frame is the latest balance of each broker and
    currency. Fiat stays there and is left out of holdings.
    """
    frames = [frame for frame in (degiro, bitvavo, ledger) if frame is not None and not frame.empty]
    if frames:
        transactions = finalize(pd.concat(frames, ignore_index=True))
    else:
        transactions = finalize(empty_frame(TRANSACTION_COLUMNS))
    from processing.calculations import build_portfolio
    from processing.enrich import enrich, latest_prices

    transactions = enrich(transactions)
    book = build_portfolio(transactions, latest_prices(transactions))
    return transactions, book.holdings, _cash_positions(book.cash_ledger)


def _cash_positions(ledger: pd.DataFrame) -> pd.DataFrame:
    from processing.models import CASH_COLUMNS

    if ledger is None or ledger.empty:
        return empty_frame(CASH_COLUMNS)
    ordered = ledger.sort_values(["date", "broker"], kind="mergesort")
    return ordered.groupby(["broker", "currency"], sort=False).tail(1).reset_index(drop=True)


def finalize(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    for column in TRANSACTION_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame.loc[:, list(TRANSACTION_COLUMNS)]
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("quantity", "price", "amount", "fees"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["fees"] = frame["fees"].fillna(0).abs()
    for column in ("broker", "type", "symbol", "isin", "name", "currency", "original_currency", "venue"):
        frame[column] = _as_text(frame[column])
    frame["broker"] = frame["broker"].str.lower()
    frame["type"] = frame["type"].str.lower()
    frame["symbol"] = frame["symbol"].str.upper()
    frame["isin"] = frame["isin"].str.upper()
    frame["currency"] = frame["currency"].str.upper().replace("", "EUR")
    frame["original_currency"] = frame["original_currency"].str.upper()
    frame["venue"] = frame["venue"].str.upper()
    missing_original = frame["original_currency"].eq("")
    frame.loc[missing_original, "original_currency"] = frame.loc[missing_original, "currency"]
    signed = frame["type"].eq("transfer")
    frame.loc[~signed, "quantity"] = frame.loc[~signed, "quantity"].fillna(0).abs()
    frame.loc[signed, "quantity"] = frame.loc[signed, "quantity"].fillna(0)
    frame = frame.dropna(subset=["date"])
    keep = frame["quantity"].abs().gt(EPS) | frame["amount"].notna()
    frame = frame.loc[keep].copy()
    frame = _merge_duplicate_trades(frame)
    frame = _merge_near_duplicate_trades(frame)
    frame = _drop_embedded_fee_rows(frame)
    frame = _merge_duplicate_cash(frame)
    frame = frame.drop_duplicates(subset=list(TRANSACTION_COLUMNS), keep="first")
    return frame.sort_values(["date", "broker"], kind="mergesort").reset_index(drop=True)


def _merge_duplicate_trades(frame: pd.DataFrame) -> pd.DataFrame:
    """Merge equivalent buy/sell rows from multiple broker exports.

    DEGIRO Account.csv and Transactions.csv can describe the same trade with
    slight differences (e.g. one row has cash amount/fees, the other does not).
    We keep one canonical row per trade key and prefer the richest values.
    """
    trade_types = {"buy", "sell"}
    mask = frame["type"].isin(trade_types)
    if not bool(mask.any()):
        return frame
    key_columns = [
        "date",
        "broker",
        "type",
        "symbol",
        "isin",
        "quantity",
        "price",
        "currency",
        "original_currency",
    ]
    drop_indexes: list[int] = []
    for _, group in frame.loc[mask].groupby(key_columns, dropna=False, sort=False):
        if len(group) <= 1:
            continue
        if not bool(group["amount"].notna().any()):
            continue
        scored = group.copy()
        scored["_score"] = (
            scored["amount"].notna().astype(int) * 4
            + scored["fees"].fillna(0).gt(0).astype(int) * 2
            + scored["isin"].fillna("").ne("").astype(int)
            + scored["name"].fillna("").astype(str).str.len() / 1000
        )
        best = scored.sort_values("_score", ascending=False, kind="mergesort").iloc[0]
        best_idx = int(best.name)
        names = [str(value).strip() for value in group["name"] if str(value).strip()]
        if names:
            frame.at[best_idx, "name"] = max(names, key=len)
        venues = [str(value).strip().upper() for value in group["venue"] if str(value).strip()]
        if venues:
            frame.at[best_idx, "venue"] = venues[0]
        fees = pd.to_numeric(group["fees"], errors="coerce").fillna(0.0)
        frame.at[best_idx, "fees"] = float(fees.max())
        if pd.isna(frame.at[best_idx, "amount"]):
            amounts = pd.to_numeric(group["amount"], errors="coerce").dropna()
            if not amounts.empty:
                frame.at[best_idx, "amount"] = float(amounts.iloc[0])
        drop_indexes.extend(int(index) for index in group.index if int(index) != best_idx)
    if drop_indexes:
        frame = frame.drop(index=drop_indexes)
    return frame


_NEAR_DUPLICATE = pd.Timedelta(minutes=5)


def _merge_near_duplicate_trades(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop a second copy of a trade when the exports disagree by a few minutes.

    DEGIRO's account file and transactions file can stamp the same order one
    minute apart. The copy without a cash amount is not a second purchase.
    Two rows that both have a cash amount stay separate.
    """
    trade_types = {"buy", "sell"}
    mask = frame["type"].isin(trade_types)
    if not bool(mask.any()):
        return frame
    trades = frame.loc[mask].copy()
    trades["_price_key"] = pd.to_numeric(trades["price"], errors="coerce").round(6)
    trades["_qty_key"] = pd.to_numeric(trades["quantity"], errors="coerce").round(8)
    key_columns = ["broker", "type", "symbol", "isin", "currency", "original_currency", "_price_key", "_qty_key"]
    drop_indexes: list[int] = []
    for _, group in trades.groupby(key_columns, dropna=False, sort=False):
        ordered = group.sort_values("date", kind="mergesort")
        consumed: set[int] = set()
        rows = list(ordered.itertuples(index=True))
        for left_pos, left in enumerate(rows):
            left_idx = int(left.Index)
            if left_idx in consumed:
                continue
            left_has_amount = pd.notna(left.amount)
            for right in rows[left_pos + 1 :]:
                right_idx = int(right.Index)
                if right_idx in consumed:
                    continue
                gap = abs(pd.Timestamp(right.date) - pd.Timestamp(left.date))
                if gap > _NEAR_DUPLICATE:
                    break
                right_has_amount = pd.notna(right.amount)
                if left_has_amount and right_has_amount and str(left.type) == "buy":
                    if _looks_like_fee_only_trade_pair(left, right):
                        keep_idx = left_idx if abs(float(left.amount)) >= abs(float(right.amount)) else right_idx
                        drop_idx = right_idx if keep_idx == left_idx else left_idx
                        names = [
                            str(value).strip()
                            for value in (left.name, right.name)
                            if str(value).strip() and str(value) != "nan"
                        ]
                        if names:
                            frame.at[keep_idx, "name"] = max(names, key=len)
                        venues = [
                            str(value).strip().upper()
                            for value in (left.venue, right.venue)
                            if str(value).strip() and str(value) != "nan"
                        ]
                        if venues:
                            frame.at[keep_idx, "venue"] = venues[0]
                        fees = [float(value) for value in (left.fees, right.fees) if pd.notna(value)]
                        if fees:
                            frame.at[keep_idx, "fees"] = max(fees)
                        drop_indexes.append(drop_idx)
                        consumed.add(drop_idx)
                        consumed.add(keep_idx)
                        break
                if left_has_amount == right_has_amount:
                    continue
                keep_idx = left_idx if left_has_amount else right_idx
                drop_idx = right_idx if left_has_amount else left_idx
                names = [str(value).strip() for value in (left.name, right.name) if str(value).strip() and str(value) != "nan"]
                if names:
                    frame.at[keep_idx, "name"] = max(names, key=len)
                venues = [
                    str(value).strip().upper()
                    for value in (left.venue, right.venue)
                    if str(value).strip() and str(value) != "nan"
                ]
                if venues:
                    frame.at[keep_idx, "venue"] = venues[0]
                fees = [float(value) for value in (left.fees, right.fees) if pd.notna(value)]
                if fees:
                    frame.at[keep_idx, "fees"] = max(fees)
                drop_indexes.append(drop_idx)
                consumed.add(drop_idx)
                consumed.add(keep_idx)
                break
    if drop_indexes:
        frame = frame.drop(index=drop_indexes)
    return frame


def _looks_like_fee_only_trade_pair(left, right) -> bool:
    left_amount = float(left.amount)
    right_amount = float(right.amount)
    if left_amount * right_amount <= 0:
        return False
    fee_gap = abs(abs(left_amount) - abs(right_amount))
    if fee_gap <= EPS:
        return False
    left_fee = float(left.fees) if pd.notna(left.fees) else 0.0
    right_fee = float(right.fees) if pd.notna(right.fees) else 0.0
    has_single_fee = (left_fee > EPS) ^ (right_fee > EPS)
    if not has_single_fee:
        return False
    expected_fee = max(left_fee, right_fee)
    return abs(fee_gap - expected_fee) <= 0.02


def _drop_embedded_fee_rows(frame: pd.DataFrame) -> pd.DataFrame:
    fee_rows = frame.loc[frame["type"].eq("fee")].copy()
    if fee_rows.empty:
        return frame
    trades = frame.loc[frame["type"].isin({"buy", "sell"})].copy()
    if trades.empty:
        return frame
    drop_indexes: list[int] = []
    for fee in fee_rows.itertuples(index=True):
        fee_amount = pd.to_numeric([fee.amount], errors="coerce")[0]
        if pd.isna(fee_amount):
            continue
        fee_abs = abs(float(fee_amount))
        if fee_abs <= EPS:
            continue
        if str(fee.broker or "").lower() != "degiro":
            continue
        candidates = trades.loc[
            trades["broker"].eq(fee.broker)
            & trades["currency"].eq(fee.currency)
            & trades["name"].eq(fee.name)
        ]
        if candidates.empty:
            continue
        for trade in candidates.itertuples(index=True):
            gap = abs(pd.Timestamp(trade.date) - pd.Timestamp(fee.date))
            if gap > _NEAR_DUPLICATE:
                continue
            trade_amount = pd.to_numeric([trade.amount], errors="coerce")[0]
            trade_price = pd.to_numeric([trade.price], errors="coerce")[0]
            trade_qty = pd.to_numeric([trade.quantity], errors="coerce")[0]
            if pd.isna(trade_amount) or pd.isna(trade_price) or pd.isna(trade_qty) or trade_qty <= EPS:
                continue
            gross = float(trade_price) * float(trade_qty)
            if str(trade.type) == "buy":
                embedded = abs(float(trade_amount)) - gross
            else:
                embedded = gross - abs(float(trade_amount))
            if abs(embedded - fee_abs) <= 0.02:
                drop_indexes.append(int(fee.Index))
                break
    if drop_indexes:
        frame = frame.drop(index=drop_indexes)
    return frame


def _merge_duplicate_cash(frame: pd.DataFrame) -> pd.DataFrame:
    """One bank deposit or withdrawal when both DEGIRO files contain it."""
    mask = frame["type"].isin({"deposit", "withdrawal"})
    if not bool(mask.any()):
        return frame
    key_columns = ["date", "broker", "type", "currency", "amount"]
    drop_indexes: list[int] = []
    for _, group in frame.loc[mask].groupby(key_columns, dropna=False, sort=False):
        if len(group) <= 1:
            continue
        best = group.iloc[0]
        best_idx = int(best.name)
        names = [str(value).strip() for value in group["name"] if str(value).strip()]
        if names:
            frame.at[best_idx, "name"] = max(names, key=len)
        drop_indexes.extend(int(index) for index in group.index if int(index) != best_idx)
    if drop_indexes:
        frame = frame.drop(index=drop_indexes)
    return frame


def degiro_venue_balances(transactions: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Replay DEGIRO trade quantities by execution venue."""
    columns = ["isin", "symbol", "name", "venue", "quantity"]
    if transactions is None or transactions.empty:
        return pd.DataFrame(columns=columns), []
    trades = transactions.loc[
        transactions["broker"].eq("degiro") & transactions["type"].isin(["buy", "sell"])
    ].copy()
    if trades.empty:
        return pd.DataFrame(columns=columns), []
    trades["quantity"] = pd.to_numeric(trades["quantity"], errors="coerce").fillna(0.0).abs()
    trades["venue"] = trades["venue"].fillna("").astype(str).str.strip().str.upper().replace("", "Unknown")
    by_venue: dict[tuple[str, str], float] = {}
    labels: dict[str, tuple[str, str]] = {}
    warnings: list[str] = []
    ordered = trades.sort_values(["date", "broker"], kind="mergesort")
    for row in ordered.itertuples(index=False):
        symbol = str(row.symbol or "").strip().upper()
        isin = str(row.isin or "").strip().upper()
        key = isin or symbol
        if not key:
            continue
        labels[key] = (isin, str(row.name or key).strip() or key)
        venue = str(row.venue or "Unknown")
        venue_key = (key, venue)
        delta = float(row.quantity)
        if str(row.type) == "sell":
            delta = -delta
        by_venue[venue_key] = by_venue.get(venue_key, 0.0) + delta
        if by_venue[venue_key] < -EPS:
            warnings.append(f"{key} sold on {venue} before recorded buys on that venue.")
    rows = []
    for (key, venue), quantity in by_venue.items():
        if quantity <= EPS:
            continue
        isin, name = labels.get(key, ("", key))
        rows.append(
            {
                "isin": isin,
                "symbol": key if not isin else "",
                "name": name,
                "venue": venue,
                "quantity": quantity,
            }
        )
    if not rows:
        return pd.DataFrame(columns=columns), sorted(set(warnings))
    frame = pd.DataFrame(rows, columns=columns)
    return frame.sort_values(["isin", "name", "venue"], kind="mergesort").reset_index(drop=True), sorted(set(warnings))


def save_cache(df: pd.DataFrame, meta: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    frame = df.copy()
    frame.to_csv(DATA_DIR / "transactions.csv", index=False)
    saved = dict(meta)
    saved["sqlite"] = False
    try:
        from sqlalchemy import create_engine

        database = DATA_DIR / "portfolio.db"
        engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
        frame.to_sql("transactions", engine, if_exists="replace", index=False)
        engine.dispose()
        saved["sqlite"] = True
    except Exception:
        saved["sqlite"] = False
    (DATA_DIR / "cache_meta.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")


def load_cache() -> tuple[pd.DataFrame | None, dict | None]:
    meta = _read_meta()
    frame = None
    if meta.get("sqlite"):
        frame = _read_sqlite()
    if frame is None:
        frame = _read_csv()
    if frame is None:
        return None, None
    from processing.enrich import enrich

    return enrich(finalize(frame)), meta


def cache_exists() -> bool:
    return (DATA_DIR / "transactions.csv").exists() or (DATA_DIR / "portfolio.db").exists()


def clear_cache() -> None:
    for name in ("transactions.csv", "portfolio.db", "cache_meta.json"):
        path = DATA_DIR / name
        if path.exists():
            path.unlink()


def read_candidates(data: bytes) -> list[pd.DataFrame]:
    if not data or not data.strip():
        raise ValueError("file is empty")
    text = data.decode("utf-8-sig", errors="replace")
    text = _extract_table(text)
    candidates: list[pd.DataFrame] = []
    for separator in (",", ";", "\t"):
        try:
            frame = pd.read_csv(
                io.StringIO(text),
                sep=separator,
                dtype=str,
                keep_default_na=False,
                engine="python",
                on_bad_lines="skip",
            )
        except Exception:
            continue
        if frame.shape[1] < 2:
            continue
        frame = _clean_columns(frame)
        mask = frame.apply(lambda column: column.str.strip().ne("")).any(axis=1)
        frame = frame.loc[mask].reset_index(drop=True)
        if not frame.empty:
            candidates.append(frame)
    if not candidates:
        raise ValueError("no table found")
    return candidates


def _extract_table(text: str) -> str:
    lines = text.splitlines()
    for index, line in enumerate(lines[:15]):
        lowered = line.lower()
        if any(key in lowered for key in _HEADER_KEYS):
            return "\n".join(lines[index:])
    return text


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    renamed = []
    for column in df.columns:
        text = str(column).strip()
        if text.lower().startswith("unnamed"):
            text = ""
        renamed.append(text)
    frame = df.copy()
    frame.columns = renamed
    return frame


def _looks_like_currency(series: pd.Series) -> bool:
    values = [str(value).strip() for value in series.head(30) if str(value).strip()]
    if not values:
        return False
    letters = sum(bool(re.fullmatch(r"[A-Za-z]{3}", value)) for value in values)
    return letters / len(values) >= 0.6


def _as_text(series: pd.Series) -> pd.Series:
    return (
        series.fillna("")
        .astype(str)
        .str.strip()
        .replace({"nan": "", "None": "", "<NA>": "", "NaT": ""})
    )


def _read_meta() -> dict:
    path = DATA_DIR / "cache_meta.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_sqlite() -> pd.DataFrame | None:
    database = DATA_DIR / "portfolio.db"
    if not database.exists():
        return None
    try:
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
        frame = pd.read_sql("SELECT * FROM transactions", engine)
        engine.dispose()
    except Exception:
        return None
    return frame


def _read_csv() -> pd.DataFrame | None:
    path = DATA_DIR / "transactions.csv"
    if not path.exists():
        return None
    try:
        return pd.read_csv(path)
    except Exception:
        return None


def cache_meta(files: list[tuple[str, str, int]]) -> dict:
    return {
        "imported_at": datetime.now().isoformat(timespec="minutes"),
        "files": [
            {"name": name, "source": source, "rows": rows}
            for name, source, rows in files
        ],
    }
