"""Replay transactions into holdings, cash, and a result.

Dividend helpers on this module:

- :func:`current_padi` is the sum of quantity times the expected annual dividend per share.
- :func:`dividend_history` groups dividend cash by month, quarter, and year.
- :func:`dividend_cagr` is the 1, 3, and 5 year growth of that cash, per position and for the portfolio.
- :func:`project_padi` projects PADI from a monthly contribution, a growth rate, and optional reinvestment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from processing.models import CASH_COLUMNS, DIVIDEND_COLUMNS, FIAT, HOLDING_COLUMNS
from processing.normalize import EPS

_QUANTITY_TYPES = {"buy", "sell", "transfer"}
FORCED_UNPRICED_SYMBOLS = frozenset({"LUNA", "BLOSK"})


@dataclass
class _Lot:
    broker: str
    instrument: str
    symbol: str
    isin: str
    name: str
    security_type: str
    quantity: float = 0.0
    cost: float = 0.0
    realized: float = 0.0


@dataclass
class Portfolio:
    positions: pd.DataFrame
    holdings: pd.DataFrame
    dividends: pd.DataFrame
    cash_ledger: pd.DataFrame
    history: pd.DataFrame
    cash: dict[str, float]
    contributions: float
    realized: float
    unrealized: float
    income: float
    fees: float
    market_value: float = 0.0
    invested: float = 0.0
    other_cash: dict[str, float] = field(default_factory=dict)
    padi: float = 0.0

    @property
    def cash_eur(self) -> float:
        return float(self.cash.get("EUR", 0.0))

    @property
    def net_worth(self) -> float:
        return self.market_value + self.cash_eur

    @property
    def result(self) -> float:
        return self.realized + self.unrealized + self.income + self.fees

    @classmethod
    def empty(cls) -> Portfolio:
        return cls(
            positions=pd.DataFrame(),
            holdings=pd.DataFrame(columns=list(HOLDING_COLUMNS)),
            dividends=pd.DataFrame(columns=list(DIVIDEND_COLUMNS)),
            cash_ledger=pd.DataFrame(columns=list(CASH_COLUMNS)),
            history=pd.DataFrame(columns=["date", "value", "deposits", "invested", "crypto_invested", "etf_invested"]),
            cash={},
            contributions=0.0,
            realized=0.0,
            unrealized=0.0,
            income=0.0,
            fees=0.0,
        )


def build_portfolio(
    transactions: pd.DataFrame,
    price_hints: dict[str, float] | None = None,
    price_details: dict[str, dict[str, str]] | None = None,
    now: datetime | None = None,
) -> Portfolio:
    if transactions is None or transactions.empty:
        return Portfolio.empty()
    frame = transactions.sort_values(["date", "broker"], kind="mergesort")
    portfolio = _replay(frame, price_hints or {}, price_details or {})
    portfolio.dividends = _dividends(frame)
    portfolio.cash_ledger = _cash_ledger(frame)
    portfolio.padi = current_padi(portfolio.holdings, expected_annual_dividends(frame))
    return portfolio


def _replay(
    frame: pd.DataFrame,
    price_hints: dict[str, float],
    price_details: dict[str, dict[str, str]],
) -> Portfolio:
    lots: dict[tuple[str, str, str], _Lot] = {}
    cash: dict[str, float] = {}
    eur_prices: dict[str, float] = {}
    native_prices: dict[str, tuple[float, str]] = {}
    price_meta: dict[str, dict[str, str]] = {}
    transfer_pool: dict[tuple[str, str], dict[str, float]] = {}
    pending_inbound: list[dict[str, object]] = []
    contributions = 0.0
    invested = 0.0
    invested_crypto = 0.0
    invested_etf = 0.0
    income = 0.0
    fees = 0.0
    snapshots: dict[pd.Timestamp, tuple[float, float, float, float, float]] = {}

    for row in frame.itertuples(index=False):
        kind = str(row.type or "")
        symbol = str(row.symbol or "")
        isin = str(row.isin or "")
        currency = str(row.currency or "EUR") or "EUR"
        quoted = str(row.original_currency or currency) or currency
        quantity = _num(row.quantity) or 0.0
        price = _num(row.price)
        amount = _num(row.amount)
        instrument = isin or symbol

        if price is not None and price > 0 and symbol not in FIAT and symbol not in FORCED_UNPRICED_SYMBOLS:
            native_prices[instrument] = (price, quoted)
            if quoted == "EUR":
                eur_prices[instrument] = price
                if not pd.isna(row.date):
                    price_meta[instrument] = {"source": "csv", "as_of": pd.Timestamp(row.date).isoformat()}

        if symbol in FIAT or instrument in FIAT:
            if amount is not None:
                cash[currency] = cash.get(currency, 0.0) + amount
                if currency == "EUR" and kind in {"deposit", "withdrawal"}:
                    contributions += amount
                elif currency == "EUR" and kind == "dividend":
                    income += amount
                elif currency == "EUR" and kind == "fee":
                    fees += amount
            _snapshot(snapshots, row.date, lots, eur_prices, contributions, invested, invested_crypto, invested_etf)
            continue

        if kind not in _QUANTITY_TYPES and kind not in {"dividend", "fee"}:
            continue

        key = (str(row.broker or ""), instrument)
        if kind in _QUANTITY_TYPES and abs(quantity) > 0:
            lot = lots.get(key)
            if lot is None:
                lot = _Lot(
                    broker=str(row.broker or ""),
                    instrument=instrument,
                    symbol=symbol,
                    isin=isin,
                    name=str(row.name or symbol),
                    security_type=_security_type(str(row.broker or ""), symbol),
                )
                lots[key] = lot
            _prefer_name(lot, str(row.name or ""))
            if kind == "buy":
                # Cash spent already includes the broker fee, so it is the cost.
                lot.quantity += abs(quantity)
                lot.cost += _spent(amount, price, quoted, abs(quantity))
                _add_cash(cash, currency, amount)
                if amount is not None:
                    spent = abs(amount)
                    invested += spent
                    if _is_crypto_broker(str(row.broker or "")):
                        invested_crypto += spent
                    else:
                        invested_etf += spent
            elif kind == "sell":
                sold = abs(quantity)
                proceeds = _proceeds(amount, price, quoted, sold)
                _realize_sale(lot, sold, proceeds)
                _add_cash(cash, currency, amount)
                if amount is not None:
                    returned = abs(amount)
                    invested -= returned
                    if _is_crypto_broker(str(row.broker or "")):
                        invested_crypto -= returned
                    else:
                        invested_etf -= returned
            elif quantity > 0:
                lot.quantity += quantity
                moved_cost, moved_qty = _consume_transfer_cost(
                    transfer_pool, str(row.broker or ""), instrument, quantity
                )
                lot.cost += moved_cost
                shortfall = quantity - moved_qty
                if shortfall > EPS:
                    if moved_qty <= EPS and price is not None and quoted == "EUR":
                        lot.cost += price * quantity
                    else:
                        pending_inbound.append(
                            {
                                "broker": str(row.broker or ""),
                                "instrument": instrument,
                                "quantity": shortfall,
                                "lot": lot,
                            }
                        )
            else:
                released_qty, released_cost = _release_cost(lot, abs(quantity))
                if released_qty > EPS and released_cost > 0:
                    given_qty, given_cost = _fill_pending_inbound(
                        pending_inbound, str(row.broker or ""), instrument, released_qty, released_cost
                    )
                    left_qty = released_qty - given_qty
                    left_cost = released_cost - given_cost
                    if left_qty > EPS and left_cost > EPS:
                        bucket = transfer_pool.setdefault((instrument, str(row.broker or "")), {"quantity": 0.0, "cost": 0.0})
                        bucket["quantity"] += left_qty
                        bucket["cost"] += left_cost
        elif kind == "dividend" and amount is not None:
            _add_cash(cash, currency, amount)
            if currency == "EUR":
                income += amount
        elif kind == "fee" and amount is not None:
            _add_cash(cash, currency, amount)
            if currency == "EUR":
                fees += amount

        _snapshot(snapshots, row.date, lots, eur_prices, contributions, invested, invested_crypto, invested_etf)

    for instrument, price in price_hints.items():
        detail = price_details.get(instrument, {})
        source = str(detail.get("source") or "").strip().lower()
        if price > 0 and source != "unpriced":
            eur_prices[instrument] = price
            native_prices[instrument] = (price, "EUR")
            price_meta[instrument] = {
                "source": source or "live",
                "as_of": str(detail.get("as_of") or ""),
            }

    _sweep_transfer_dust(transfer_pool, lots)
    positions = _positions_frame(lots, eur_prices, native_prices, price_meta)
    market_value = float(positions["current_value"].sum()) if not positions.empty else 0.0
    unrealized = float(positions["unrealized"].sum()) if not positions.empty else 0.0
    realized = sum(lot.realized for lot in lots.values())
    history = _history_frame(snapshots)
    if not history.empty:
        history.loc[history.index[-1], "value"] = market_value
    other = {code: amount for code, amount in cash.items() if code != "EUR" and abs(amount) >= 0.01}
    return Portfolio(
        positions=positions,
        holdings=_holdings_frame(positions),
        dividends=pd.DataFrame(columns=list(DIVIDEND_COLUMNS)),
        cash_ledger=pd.DataFrame(columns=list(CASH_COLUMNS)),
        history=history,
        cash=cash,
        contributions=contributions,
        realized=realized,
        unrealized=unrealized,
        income=income,
        fees=fees,
        market_value=market_value,
        invested=invested,
        other_cash=other,
    )


def _positions_frame(
    lots: dict[tuple[str, str, str], _Lot],
    eur_prices: dict[str, float],
    native_prices: dict[str, tuple[float, str]],
    price_meta: dict[str, dict[str, str]],
) -> pd.DataFrame:
    rows = []
    for lot in lots.values():
        if abs(lot.quantity) < EPS:
            continue
        meta = price_meta.get(lot.instrument, {})
        source = str(meta.get("source") or "")
        as_of = str(meta.get("as_of") or "")
        force_unpriced = lot.symbol in FORCED_UNPRICED_SYMBOLS or source == "unpriced"
        price = eur_prices.get(lot.instrument)
        currency = "EUR"
        valued_at_cost = price is None
        if force_unpriced:
            price = None
            valued_at_cost = False
        elif price is None:
            native = native_prices.get(lot.instrument)
            if native and native[1] == "EUR":
                price, currency = native
                valued_at_cost = False
                if not source:
                    source = "csv"
            if not as_of:
                native_meta = price_meta.get(lot.instrument, {})
                as_of = str(native_meta.get("as_of") or "")
        unpriced = force_unpriced
        if unpriced:
            market_value = math.nan
            shown_price = math.nan
            valued_at_cost = False
        elif price is None:
            market_value = lot.cost
            shown_price = math.nan
            source = "cost"
        else:
            market_value = lot.quantity * price
            shown_price = price
        rows.append(
            {
                "broker": lot.broker,
                "symbol": lot.symbol,
                "isin": lot.isin,
                "name": lot.name,
                "security_type": lot.security_type,
                "quantity": lot.quantity,
                "cost": lot.cost,
                "avg_cost": lot.cost / lot.quantity if abs(lot.quantity) > EPS else math.nan,
                "price": shown_price,
                "current_value": market_value,
                "currency": currency if not valued_at_cost else "EUR",
                "unrealized": 0.0 if valued_at_cost or unpriced else market_value - lot.cost,
                "valued_at_cost": valued_at_cost,
                "price_source": source or ("cost" if valued_at_cost else "csv"),
                "price_as_of": as_of,
                "unpriced": unpriced,
            }
        )
    if not rows:
        return pd.DataFrame()
    positions = pd.DataFrame(rows).sort_values("current_value", ascending=False, kind="mergesort")
    positive = positions.loc[positions["current_value"] > 0, "current_value"].sum()
    positions["weight"] = positions["current_value"] / positive * 100 if positive else 0.0
    return positions.reset_index(drop=True)


def _history_frame(snapshots: dict[pd.Timestamp, tuple[float, float, float, float, float]]) -> pd.DataFrame:
    if not snapshots:
        return pd.DataFrame(columns=["date", "value", "deposits", "invested", "crypto_invested", "etf_invested"])
    ordered = sorted(snapshots)
    return pd.DataFrame(
        {
            "date": ordered,
            "value": [snapshots[day][0] for day in ordered],
            "deposits": [snapshots[day][1] for day in ordered],
            "invested": [snapshots[day][2] for day in ordered],
            "crypto_invested": [snapshots[day][3] for day in ordered],
            "etf_invested": [snapshots[day][4] for day in ordered],
        }
    )


def _is_crypto_broker(broker: str) -> bool:
    return broker.strip().lower() in {"bitvavo", "ledger"}


def _snapshot(
    snapshots: dict[pd.Timestamp, tuple[float, float, float, float, float]],
    when: object,
    lots: dict[tuple[str, str, str], _Lot],
    eur_prices: dict[str, float],
    contributions: float,
    invested: float,
    invested_crypto: float,
    invested_etf: float,
) -> None:
    if pd.isna(when):
        return
    day = pd.Timestamp(when).normalize()
    value = 0.0
    for lot in lots.values():
        if abs(lot.quantity) < EPS:
            continue
        price = eur_prices.get(lot.instrument)
        value += lot.cost if price is None else lot.quantity * price
    snapshots[day] = (value, contributions, invested, invested_crypto, invested_etf)


def _realize_sale(lot: _Lot, quantity: float, proceeds: float) -> None:
    if lot.quantity > EPS:
        closing = min(quantity, lot.quantity)
        removed = lot.cost * (closing / lot.quantity)
        attributed = proceeds * (closing / quantity) if quantity else proceeds
        lot.realized += attributed - removed
        lot.cost -= removed
        lot.quantity -= closing
        leftover = quantity - closing
        if leftover > EPS:
            lot.quantity -= leftover
            lot.realized += proceeds * (leftover / quantity) if quantity else 0.0
    else:
        lot.quantity -= quantity
        lot.realized += proceeds
    if abs(lot.quantity) < EPS:
        lot.quantity = 0.0
        lot.cost = 0.0


def _release_cost(lot: _Lot, quantity: float) -> tuple[float, float]:
    """A withdrawal moves coins out. It is not a sale, so it does not book profit."""
    moved_quantity = 0.0
    moved_cost = 0.0
    if lot.quantity > EPS:
        closing = min(quantity, lot.quantity)
        moved_quantity = closing
        moved_cost = lot.cost * (closing / lot.quantity)
        lot.cost -= moved_cost
        lot.quantity -= quantity
    else:
        lot.quantity -= quantity
    if abs(lot.quantity) < EPS:
        lot.quantity = 0.0
        lot.cost = 0.0
    return moved_quantity, moved_cost


def _consume_transfer_cost(
    pool: dict[tuple[str, str], dict[str, float]],
    target_broker: str,
    instrument: str,
    quantity: float,
) -> tuple[float, float]:
    if quantity <= EPS:
        return 0.0, 0.0
    remaining = quantity
    consumed = 0.0
    taken = 0.0
    for key, bucket in list(pool.items()):
        entry_instrument, source_broker = key
        if entry_instrument != instrument or source_broker == target_broker:
            continue
        available_qty = float(bucket.get("quantity", 0.0))
        available_cost = float(bucket.get("cost", 0.0))
        if available_qty <= EPS or available_cost <= 0:
            continue
        taken_qty = min(remaining, available_qty)
        dust_qty = available_qty - taken_qty
        if dust_qty <= max(EPS, available_qty * 0.02):
            taken_cost = available_cost
            taken_qty = available_qty
            dust_qty = 0.0
        else:
            taken_cost = available_cost * (taken_qty / available_qty)
        bucket["quantity"] = max(0.0, available_qty - taken_qty)
        bucket["cost"] = max(0.0, available_cost - taken_cost)
        consumed += taken_cost
        taken += min(taken_qty, remaining)
        remaining -= min(taken_qty, remaining)
        if bucket["quantity"] <= EPS or bucket["cost"] <= EPS:
            pool.pop(key, None)
        if remaining <= EPS:
            break
    return consumed, taken


def _sweep_transfer_dust(pool: dict[tuple[str, str], dict[str, float]], lots: dict[tuple[str, str, str], _Lot]) -> None:
    """Network fees leave a little quantity and cost with no receiving lot."""
    for key, bucket in list(pool.items()):
        instrument, source_broker = key
        quantity = float(bucket.get("quantity", 0.0))
        cost = float(bucket.get("cost", 0.0))
        if quantity <= EPS or cost <= EPS:
            pool.pop(key, None)
            continue
        candidates = [
            lot
            for lot in lots.values()
            if lot.instrument == instrument and lot.broker != source_broker and lot.quantity > quantity
        ]
        if not candidates:
            continue
        target = max(candidates, key=lambda lot: lot.quantity)
        if quantity > target.quantity * 0.05:
            continue
        target.cost += cost
        pool.pop(key, None)


def _fill_pending_inbound(
    pending: list[dict[str, object]],
    source_broker: str,
    instrument: str,
    quantity: float,
    cost: float,
) -> tuple[float, float]:
    """Give withdrawal cost to receipts that were booked before the send."""
    remaining_qty = quantity
    remaining_cost = cost
    given_qty = 0.0
    given_cost = 0.0
    for entry in pending:
        if remaining_qty <= EPS or remaining_cost <= EPS:
            break
        if entry.get("instrument") != instrument or entry.get("broker") == source_broker:
            continue
        waiting = float(entry.get("quantity") or 0.0)
        if waiting <= EPS:
            continue
        take = min(waiting, remaining_qty)
        dust = remaining_qty - take
        if dust <= max(EPS, quantity * 0.02):
            share = remaining_cost
            take_for_pool = remaining_qty
        else:
            share = remaining_cost * (take / remaining_qty)
            take_for_pool = take
        lot = entry["lot"]
        lot.cost = float(lot.cost) + share
        entry["quantity"] = waiting - take
        given_qty += take
        given_cost += share
        remaining_qty -= take_for_pool
        remaining_cost -= share
    return given_qty, given_cost


def _spent(amount: float | None, price: float | None, quoted: str, quantity: float) -> float:
    if amount is not None:
        return abs(amount)
    if price is not None and quoted == "EUR":
        return price * quantity
    return 0.0


def _proceeds(cashflow: float | None, price: float | None, price_currency: str, quantity: float) -> float:
    if cashflow is not None:
        return abs(cashflow)
    if price is not None and (price_currency or "EUR") == "EUR":
        return price * quantity
    return 0.0


def _add_cash(cash: dict[str, float], currency: str, cashflow: float | None) -> None:
    if cashflow is None:
        return
    cash[currency] = cash.get(currency, 0.0) + cashflow


def _security_type(broker: str, symbol: str) -> str:
    if symbol in FIAT:
        return ""
    if broker == "bitvavo":
        from importers.bitvavo import SECURITY_TYPE

        return SECURITY_TYPE
    if broker == "ledger":
        from importers.ledger import SECURITY_TYPE

        return SECURITY_TYPE
    if broker == "degiro":
        return "equity"
    return "Crypto"


def _prefer_name(lot: _Lot, name: str) -> None:
    if not name:
        return
    if not lot.name or (lot.name.upper() == lot.symbol and name.upper() != lot.symbol):
        lot.name = name


def _holdings_frame(positions: pd.DataFrame) -> pd.DataFrame:
    columns = list(HOLDING_COLUMNS)
    if positions.empty:
        return pd.DataFrame(columns=columns)
    frame = positions.copy()
    frame["key"] = frame["isin"].where(frame["isin"].fillna("").ne(""), frame["symbol"])
    rows = []
    for _, group in frame.groupby("key", sort=False):
        quantity = float(group["quantity"].sum())
        cost = float(group["cost"].sum())
        isin = str(group["isin"].iloc[0] or "")
        symbols = [str(value) for value in group["symbol"] if str(value) and str(value) != isin]
        symbol = symbols[0] if symbols else str(group["symbol"].iloc[0])
        names = [str(value) for value in group["name"] if str(value)]
        name = max(names, key=len) if names else symbol
        currencies = {str(value) for value in group["currency"] if str(value)}
        security_type = str(group["security_type"].iloc[0] or "")
        brokers = {str(value).strip().lower() for value in group["broker"] if str(value).strip()}
        rows.append(
            {
                "symbol": symbol,
                "isin": isin,
                "name": name,
                "quantity": quantity,
                "avg_cost": cost / quantity if abs(quantity) > EPS else math.nan,
                "current_value": _sum_known(group["current_value"]),
                "currency": "EUR" if currencies != {"EUR"} and len(currencies) != 1 else (next(iter(currencies)) if currencies else "EUR"),
                "sector": "",
                "industry": "",
                "country": "Global" if security_type == "Crypto" else "",
                "exchange": _crypto_exchange(brokers, security_type),
                "security_type": security_type,
            }
        )
    holdings = pd.DataFrame(rows, columns=columns)
    return holdings.sort_values("current_value", ascending=False, kind="mergesort").reset_index(drop=True)


def _crypto_exchange(brokers: set[str], security_type: str) -> str:
    if security_type != "Crypto":
        return ""
    if brokers == {"bitvavo"}:
        return "Bitvavo"
    if brokers == {"ledger"}:
        return "Ledger"
    if brokers and brokers.issubset({"bitvavo", "ledger"}):
        return "Bitvavo/Ledger"
    return ""


def _dividends(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "type" not in frame.columns:
        return pd.DataFrame(columns=list(DIVIDEND_COLUMNS))
    rows = frame.loc[frame["type"].eq("dividend"), list(DIVIDEND_COLUMNS)]
    return rows.sort_values("date", kind="mergesort").reset_index(drop=True)


_HISTORY_COLUMNS = ("period", "symbol", "isin", "name", "currency", "amount")
_CAGR_COLUMNS = ("symbol", "isin", "name", "currency", "cagr_1y", "cagr_3y", "cagr_5y")
_PROJECTION_COLUMNS = (
    "year",
    "starting_padi",
    "contribution",
    "dividends",
    "reinvested",
    "invested",
    "padi",
)


def current_padi(holdings: pd.DataFrame, per_share: pd.Series | dict[str, float] | None = None) -> float:
    """Sum of current quantity times the expected annual dividend per share."""
    if holdings is None or holdings.empty:
        return 0.0
    rates = per_share if per_share is not None else {}
    total = 0.0
    has_column = "expected_annual_dividend" in holdings.columns
    for row in holdings.itertuples(index=False):
        quantity = _num(getattr(row, "quantity", None)) or 0.0
        supplied = _num(getattr(row, "expected_annual_dividend", None)) if has_column else None
        if supplied is not None:
            rate = supplied
        else:
            key = str(getattr(row, "isin", "") or "") or str(getattr(row, "symbol", "") or "")
            raw = rates[key] if key in getattr(rates, "index", rates) else None
            rate = _num(raw) or 0.0
        total += quantity * rate
    return total


def expected_annual_dividends(transactions: pd.DataFrame) -> pd.Series:
    """Trailing-year dividend per share, keyed by ISIN or symbol.

    Each payment is divided by the shares held at that broker on the payment
    date. Payments in the year ending on that instrument's latest dividend are
    added together. Brokers that both paid are averaged so the same dividend
    is not counted twice.
    """
    if transactions is None or transactions.empty:
        return pd.Series(dtype=float)
    quantities: dict[tuple[str, str], float] = {}
    payments: list[tuple[pd.Timestamp, str, str, float]] = []
    frame = transactions.sort_values(["date", "broker"], kind="mergesort")
    for row in frame.itertuples(index=False):
        symbol = str(getattr(row, "symbol", "") or "")
        isin = str(getattr(row, "isin", "") or "")
        instrument = isin or symbol
        if not instrument or symbol in FIAT or instrument in FIAT:
            continue
        broker = str(getattr(row, "broker", "") or "")
        kind = str(getattr(row, "type", "") or "")
        quantity = _num(getattr(row, "quantity", None)) or 0.0
        key = (broker, instrument)
        if kind == "buy":
            quantities[key] = quantities.get(key, 0.0) + abs(quantity)
        elif kind == "sell":
            quantities[key] = quantities.get(key, 0.0) - abs(quantity)
        elif kind == "transfer":
            quantities[key] = quantities.get(key, 0.0) + quantity
        elif kind == "dividend":
            held = quantities.get(key, 0.0)
            amount = _num(getattr(row, "amount", None))
            currency = str(getattr(row, "currency", "") or "EUR").upper() or "EUR"
            if amount is None or held <= EPS or currency != "EUR":
                continue
            payments.append((pd.Timestamp(row.date), broker, instrument, amount / held))
    if not payments:
        return pd.Series(dtype=float)
    paid = pd.DataFrame(payments, columns=["date", "broker", "instrument", "per_share"])
    rates: dict[str, float] = {}
    for instrument, group in paid.groupby("instrument", sort=False):
        broker_rates = []
        for _, broker_rows in group.groupby("broker", sort=False):
            anchor = broker_rows["date"].max()
            window = broker_rows.loc[broker_rows["date"] > anchor - pd.DateOffset(years=1), "per_share"]
            broker_rates.append(float(window.sum()))
        rates[str(instrument)] = sum(broker_rates) / len(broker_rates)
    return pd.Series(rates, dtype=float)


def dividend_history(dividends: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Dividend cash grouped by month, quarter, and year."""
    empty = {freq: pd.DataFrame(columns=list(_HISTORY_COLUMNS)) for freq in ("month", "quarter", "year")}
    if dividends is None or dividends.empty:
        return empty
    frame = dividends.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"])
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(0.0)
    frame = frame.loc[frame["amount"].abs().gt(EPS)]
    if frame.empty:
        return empty
    frame["symbol"] = frame["symbol"].fillna("").astype(str)
    frame["isin"] = frame["isin"].fillna("").astype(str)
    frame["name"] = frame["name"].fillna("").astype(str)
    frame["currency"] = frame["currency"].fillna("").astype(str).str.upper().replace("", "EUR")
    stamps = pd.to_datetime(frame["date"])
    grouped = {
        "month": stamps.dt.strftime("%Y-%m"),
        "quarter": stamps.dt.to_period("Q").astype(str),
        "year": stamps.dt.strftime("%Y"),
    }
    history = {}
    for freq, period in grouped.items():
        detail = frame.assign(period=period)
        rows = (
            detail.groupby(["period", "symbol", "isin", "name", "currency"], dropna=False, sort=False)["amount"]
            .sum()
            .reset_index()
        )
        history[freq] = (
            rows.loc[:, list(_HISTORY_COLUMNS)]
            .sort_values(["period", "symbol"], kind="mergesort")
            .reset_index(drop=True)
        )
    return history


def dividend_cagr(dividends: pd.DataFrame) -> pd.DataFrame:
    """1-year, 3-year, and 5-year dividend CAGR per position and for the portfolio.

    The end year is the latest calendar year with a dividend. A missing start
    year is left blank. The portfolio row sums euro dividends only.
    """
    yearly = dividend_history(dividends)["year"]
    if yearly.empty:
        return pd.DataFrame(columns=list(_CAGR_COLUMNS))
    rows = [_cagr_row(group) for _, group in yearly.groupby(["symbol", "isin", "name", "currency"], sort=False)]
    euros = yearly.loc[yearly["currency"].eq("EUR")]
    if not euros.empty:
        totals = euros.groupby("period", sort=False)["amount"].sum().reset_index()
        totals["symbol"] = "Portfolio"
        totals["isin"] = ""
        totals["name"] = "Portfolio"
        totals["currency"] = "EUR"
        rows.append(_cagr_row(totals))
    return pd.DataFrame(rows, columns=list(_CAGR_COLUMNS))


def project_padi(
    current_padi: float,
    monthly_contribution: float,
    growth_rate: float,
    years: int,
    reinvest: bool,
) -> pd.DataFrame:
    """Project PADI forward.

    ``growth_rate`` grows the dividend stream each year and is also the yield
    that turns new cash into PADI. With reinvestment, that year's dividends
    are invested together with the monthly contribution.
    """
    padi = float(current_padi)
    rows = [
        {
            "year": 0,
            "starting_padi": padi,
            "contribution": 0.0,
            "dividends": 0.0,
            "reinvested": 0.0,
            "invested": 0.0,
            "padi": padi,
        }
    ]
    for year in range(1, max(int(years), 0) + 1):
        starting = padi
        contribution = float(monthly_contribution) * 12
        reinvested = starting if reinvest else 0.0
        invested = contribution + reinvested
        padi = starting * (1 + float(growth_rate)) + invested * float(growth_rate)
        rows.append(
            {
                "year": year,
                "starting_padi": starting,
                "contribution": contribution,
                "dividends": starting,
                "reinvested": reinvested,
                "invested": invested,
                "padi": padi,
            }
        )
    return pd.DataFrame(rows, columns=list(_PROJECTION_COLUMNS))


def _cagr_row(group: pd.DataFrame) -> dict[str, object]:
    amounts = {int(period): float(amount) for period, amount in zip(group["period"], group["amount"])}
    end = max(amounts)
    first = group.iloc[0]

    def rate(span: int) -> float:
        start = amounts.get(end - span)
        finish = amounts.get(end)
        if start is None or finish is None or start <= EPS or finish < 0:
            return math.nan
        return (finish / start) ** (1 / span) - 1

    return {
        "symbol": first["symbol"],
        "isin": first["isin"],
        "name": first["name"],
        "currency": first["currency"],
        "cagr_1y": rate(1),
        "cagr_3y": rate(3),
        "cagr_5y": rate(5),
    }


def _cash_ledger(frame: pd.DataFrame) -> pd.DataFrame:
    columns = list(CASH_COLUMNS)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    moving = frame.loc[frame["amount"].notna() & frame["amount"].abs().gt(EPS)].copy()
    if moving.empty:
        return pd.DataFrame(columns=columns)
    balances: dict[tuple[str, str], float] = {}
    rows = []
    for row in moving.sort_values(["date", "broker"], kind="mergesort").itertuples(index=False):
        key = (str(row.broker), str(row.currency))
        balances[key] = balances.get(key, 0.0) + float(row.amount)
        rows.append(
            {
                "date": row.date,
                "broker": row.broker,
                "currency": row.currency,
                "amount": float(row.amount),
                "balance": balances[key],
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _sum_known(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce")
    if not bool(numeric.notna().any()):
        return math.nan
    return float(numeric.sum())


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return number
