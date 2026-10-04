"""Parser and valuation checks for the CSV portfolio pipeline."""

from __future__ import annotations

import json
import math
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

from processing.calculations import (
    build_portfolio,
    current_padi,
    dividend_cagr,
    dividend_history,
    project_padi,
)
from processing.enrich import _crypto_live_entry, latest_prices, price_hints_with_details, price_hints_with_sources
from processing.lookthrough import apply_lookthrough, load_etf_composition
from processing.models import CASH_COLUMNS, DIVIDEND_COLUMNS, HOLDING_COLUMNS, TRANSACTION_COLUMNS, make_row
from processing.normalize import (
    degiro_venue_balances,
    finalize,
    load_cache,
    normalize,
    parse_uploads,
    records_to_frame,
    save_cache,
)


DEGIRO_TRADES = """Datum,Tijd,Product,ISIN,Beurs,Uitvoeringsplaats,Aantal,Koers,,Lokale waarde,,Waarde,,Wisselkoers,Transactiekosten en/of,,Totaal,,Order ID
15-01-2024,09:05,Vanguard FTSE All-World,IE00B3RBWM25,EAM,XAMS,10,100.00,EUR,1000.00,EUR,1000.00,EUR,,1.00,EUR,-1001.00,EUR,ORD1
20-06-2024,11:00,Vanguard FTSE All-World,IE00B3RBWM25,EAM,XAMS,-4,120.00,EUR,-480.00,EUR,-480.00,EUR,,1.00,EUR,479.00,EUR,ORD2
""".encode()

DEGIRO_ACCOUNT = """Datum;Tijd;Valutadatum;Product;ISIN;Omschrijving;FX;Mutatie;;Saldo;;Order Id
01-02-2024;08:00;01-02-2024;;;iDEAL storting;;1.000,00;EUR;1.000,00;EUR;
02-02-2024;10:00;02-02-2024;Vanguard FTSE All-World;IE00B3RBWM25;Koop 10 @ 100,00 EUR;;-1.001,00;EUR;-1,00;EUR;99
03-02-2024;10:00;03-02-2024;Vanguard FTSE All-World;IE00B3RBWM25;Dividend;;20,00;EUR;19,00;EUR;
""".encode()

DEGIRO_DUPLICATE_BUY_TRADES = """Datum,Tijd,Product,ISIN,Beurs,Uitvoeringsplaats,Aantal,Koers,,Lokale waarde,,Waarde,,Wisselkoers,Transactiekosten en/of,,Totaal,,Order ID
02-02-2024,10:00,Vanguard FTSE All-World,IE00B3RBWM25,EAM,XAMS,10,100.00,EUR,1000.00,EUR,1000.00,EUR,,1.00,EUR,-1001.00,EUR,ORDX
""".encode()

DEGIRO_DUPLICATE_BUY_ACCOUNT = """Datum;Tijd;Valutadatum;Product;ISIN;Omschrijving;FX;Mutatie;;Saldo;;Order Id
02-02-2024;10:00;02-02-2024;Vanguard FTSE All-World;IE00B3RBWM25;Koop 10 @ 100,00 EUR;;-1.001,00;EUR;-1,00;EUR;ORDX
""".encode()

DEGIRO_ACCOUNT_SWAPPED = """Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id
01-02-2024,08:00,01-02-2024,,,,,EUR,1000.00,EUR,1000.00,
02-02-2024,10:00,02-02-2024,Vanguard FTSE All-World,IE00B3RBWM25,Koop 1 @ 100,00 EUR,,EUR,-100.00,EUR,900.00,99
02-02-2024,10:00,02-02-2024,Vanguard FTSE All-World,IE00B3RBWM25,DEGIRO Transactiekosten en/of kosten van derden,,EUR,-3.00,EUR,897.00,99
03-02-2024,10:00,03-02-2024,Vanguard FTSE All-World,IE00B3RBWM25,Dividend,,EUR,12.50,EUR,909.50,
03-02-2024,10:01,03-02-2024,Vanguard FTSE All-World,IE00B3RBWM25,Dividendbelasting,,EUR,-1.88,EUR,907.62,
03-02-2024,10:02,03-02-2024,,,Degiro Cash Sweep Transfer,,EUR,-50.00,EUR,857.62,
""".encode()

PARTIAL_ACCOUNT = """Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id
03-04-2024,10:00,03-04-2024,Vanguard,IE00B3RBWM25,Dividend,,20.00,EUR,1019.00,EUR,
""".encode()

BITVAVO = """Timezone,Date,Time,Type,Currency,Amount,Quote Currency,Quote Price,Received / Paid Currency,Received / Paid Amount,Fee currency,Fee amount,Status,Transaction ID,Address
Europe/Amsterdam,2024-03-01,09:00,deposit,EUR,50000,,,,,,,Completed,d1,
Europe/Amsterdam,2024-03-01,09:05,buy,BTC,1,EUR,50000,EUR,50000,EUR,0,Completed,b1,
Europe/Amsterdam,2024-03-02,12:00,withdrawal,BTC,1,,,,,,,Completed,w1,
Europe/Amsterdam,2024-04-01,12:00,buy,BTC,0.001,EUR,60000,EUR,60,EUR,0,Completed,b2,
Europe/Amsterdam,2024-04-01,12:05,sell,BTC,0.001,EUR,60000,EUR,60,EUR,0,Completed,s2,
Europe/Amsterdam,2024-04-02,12:00,buy,ETH,1,EUR,2000,EUR,2000,EUR,1,Pending,skip,
""".encode()

LEDGER = """Operation Date,Status,Currency Ticker,Operation Type,Operation Amount,Operation Fees,Operation Hash,Account Name,Account xpub,Countervalue Ticker,Countervalue at Operation Date,Countervalue at CSV Export
2024-03-02T18:00:00Z,Confirmed,BTC,IN,1,0,abc,Bitcoin,,EUR,50000,60000
""".encode()

LEDGER_COUNTERVALUE_DRIFT = """Operation Date,Status,Currency Ticker,Operation Type,Operation Amount,Operation Fees,Operation Hash,Account Name,Account xpub,Countervalue Ticker,Countervalue at Operation Date,Countervalue at CSV Export
2024-03-02T18:00:00Z,Confirmed,BTC,IN,1,0,abc,Bitcoin,,EUR,70000,70000
""".encode()

BITVAVO_FEE = """Date,Time,Type,Currency,Amount,Quote Currency,Quote Price,Received / Paid Currency,Received / Paid Amount,Fee currency,Fee amount,Status,Transaction ID
2024-05-01,10:00,buy,ETH,2,EUR,100,EUR,200,EUR,1,Completed,fee-extra
2024-05-02,10:00,buy,ETH,1,EUR,100,EUR,101,EUR,1,Completed,fee-inside
""".encode()


class PipelineTest(unittest.TestCase):
    def test_degiro_trades_split_profit_between_realized_and_unrealized(self) -> None:
        result = parse_uploads([("Transactions.csv", DEGIRO_TRADES)])
        self.assertEqual(result.warnings, [])
        self.assertEqual(list(result.transactions.columns), list(TRANSACTION_COLUMNS))
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertEqual(list(book.holdings.columns), list(HOLDING_COLUMNS))
        self.assertEqual(len(book.holdings), 1)
        holding = book.holdings.iloc[0]
        self.assertAlmostEqual(holding["quantity"], 6)
        self.assertAlmostEqual(holding["avg_cost"] * holding["quantity"], 600.6)
        self.assertAlmostEqual(holding["current_value"], 720)
        self.assertAlmostEqual(book.positions.iloc[0]["unrealized"], 119.4)
        self.assertAlmostEqual(book.realized, 78.6)
        self.assertAlmostEqual(book.result, 198)
        self.assertAlmostEqual(book.cash_eur, -522)
        self.assertAlmostEqual(book.invested, 522)

    def test_degiro_english_account_splits_fx_from_deposits_and_dividend_tax(self) -> None:
        csv = """Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id
01-03-2024,10:00,01-03-2024,,,Deposit,,1000.00,EUR,1000.00,EUR,
02-03-2024,10:00,02-03-2024,,,FX Debit,,-100.00,USD,-100.00,USD,
02-03-2024,10:00,02-03-2024,,,FX Credit,,92.00,EUR,1092.00,EUR,
03-03-2024,11:00,03-03-2024,Apple,US0378331005,Buy 2 @ 150.00 USD,,-280.00,EUR,812.00,EUR,
03-03-2024,12:00,03-03-2024,Apple,US0378331005,Dividend tax,,-3.00,EUR,809.00,EUR,
03-03-2024,12:05,03-03-2024,Apple,US0378331005,Dividend,,15.00,EUR,824.00,EUR,
04-03-2024,09:00,04-03-2024,,,Withdrawal,,-24.00,EUR,800.00,EUR,
""".encode()
        result = parse_uploads([("Account.csv", csv)])
        self.assertEqual(result.warnings, [])
        kinds = result.transactions.sort_values("date")["type"].tolist()
        self.assertEqual(kinds, ["deposit", "transfer", "transfer", "buy", "fee", "dividend", "withdrawal"])
        buy = result.transactions.loc[result.transactions["type"].eq("buy")].iloc[0]
        self.assertEqual(buy["original_currency"], "USD")
        self.assertEqual(buy["currency"], "EUR")
        self.assertAlmostEqual(buy["quantity"], 2)
        self.assertAlmostEqual(buy["price"], 150)
        self.assertAlmostEqual(buy["amount"], -280)
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertAlmostEqual(book.dividends.iloc[0]["amount"], 15)
        self.assertAlmostEqual(book.fees, -3)
        self.assertAlmostEqual(book.contributions, 976)
        self.assertAlmostEqual(book.cash.get("USD"), -100)
        self.assertAlmostEqual(book.cash_eur, 800)

    def test_degiro_dutch_account_reads_valuta_conversion_with_comma_decimals(self) -> None:
        csv = """Datum;Tijd;Valutadatum;Product;ISIN;Omschrijving;FX;Mutatie;;Saldo;;Order Id
02-03-2024;10:00;02-03-2024;;;Valuta Debitering;;-1.000,00;USD;-1.000,00;USD;
02-03-2024;10:00;02-03-2024;;;Valuta Creditering;;920,50;EUR;920,50;EUR;
""".encode()
        result = parse_uploads([("Account.csv", csv)])
        self.assertEqual(result.warnings, [])
        rows = result.transactions.sort_values(["currency", "amount"])
        self.assertEqual(rows["type"].tolist(), ["transfer", "transfer"])
        usd = rows.loc[rows["currency"].eq("USD")].iloc[0]
        eur = rows.loc[rows["currency"].eq("EUR")].iloc[0]
        self.assertAlmostEqual(usd["amount"], -1000)
        self.assertAlmostEqual(eur["amount"], 920.5)
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertAlmostEqual(book.contributions, 0)
        self.assertAlmostEqual(book.cash_eur, 920.5)
        self.assertAlmostEqual(book.cash.get("USD"), -1000)

    def test_degiro_english_transactions_keep_the_price_currency(self) -> None:
        csv = """Date,Time,Product,ISIN,Exchange,Execution venue,Quantity,Price,,Local value,,Value,,FX rate,Transaction costs,,Total,,Order ID
15-01-2024,09:05,Apple,US0378331005,NSY,XNAS,2,"150,00",USD,"300,00",USD,"280,00",EUR,,"1,00",EUR,"-281,00",EUR,1
""".encode()
        result = parse_uploads([("Transactions.csv", csv)])
        self.assertEqual(result.warnings, [])
        trade = result.transactions.iloc[0]
        self.assertEqual(trade["type"], "buy")
        self.assertEqual(trade["isin"], "US0378331005")
        self.assertEqual(trade["original_currency"], "USD")
        self.assertEqual(trade["currency"], "EUR")
        self.assertAlmostEqual(trade["price"], 150)
        self.assertAlmostEqual(trade["fees"], 1)
        self.assertAlmostEqual(trade["amount"], -281)
        self.assertEqual(trade["venue"], "NSY")

    def test_degiro_account_statement_reads_dutch_decimals(self) -> None:
        result = parse_uploads([("Account.csv", DEGIRO_ACCOUNT)])
        self.assertEqual(result.warnings, [])
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertAlmostEqual(book.holdings.iloc[0]["quantity"], 10)
        self.assertAlmostEqual(book.market_value, 1000)
        self.assertEqual(list(book.dividends.columns), list(DIVIDEND_COLUMNS))
        self.assertAlmostEqual(book.dividends.iloc[0]["amount"], 20)
        self.assertEqual(list(book.cash_ledger.columns), list(CASH_COLUMNS))
        self.assertAlmostEqual(book.cash_ledger.iloc[-1]["balance"], 19)
        self.assertAlmostEqual(book.income, 20)
        self.assertAlmostEqual(book.result, 19)
        self.assertAlmostEqual(book.cash_eur, 19)
        self.assertAlmostEqual(book.contributions, 1000)
        self.assertAlmostEqual(book.net_worth - book.contributions, book.result)

    def test_history_tracks_holdings_value_not_cash_and_ends_at_market_value(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9, 0),
                    broker="degiro",
                    type="deposit",
                    symbol="EUR",
                    name="EUR",
                    amount=1000,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 2, 9, 0),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3RBWM25",
                    isin="IE00B3RBWM25",
                    name="Vanguard FTSE All-World",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        book = build_portfolio(rows, {"IE00B3RBWM25": 120}, {"IE00B3RBWM25": {"source": "live", "as_of": "2024-01-03"}})
        history = book.history.sort_values("date").reset_index(drop=True)
        self.assertEqual(len(history), 2)
        self.assertAlmostEqual(float(history.iloc[0]["deposits"]), 1000)
        self.assertAlmostEqual(float(history.iloc[1]["deposits"]), 1000)
        self.assertAlmostEqual(float(history.iloc[0]["invested"]), 0)
        self.assertAlmostEqual(float(history.iloc[1]["invested"]), 100)
        self.assertAlmostEqual(float(history.iloc[1]["etf_invested"]), 100)
        self.assertAlmostEqual(float(history.iloc[1]["crypto_invested"]), 0)
        self.assertAlmostEqual(float(history.iloc[0]["value"]), 0)
        self.assertAlmostEqual(float(history.iloc[1]["value"]), 120)
        self.assertAlmostEqual(book.market_value, 120)

    def test_history_has_invested_or_deposit_signal_for_each_broker_type(self) -> None:
        degiro = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9, 0),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3RBWM25",
                    isin="IE00B3RBWM25",
                    name="Vanguard FTSE All-World",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        bitvavo = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9, 0),
                    broker="bitvavo",
                    type="deposit",
                    symbol="EUR",
                    name="EUR",
                    amount=500,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 1, 9, 1),
                    broker="bitvavo",
                    type="buy",
                    symbol="BTC",
                    name="BTC",
                    quantity=0.01,
                    price=50000,
                    amount=-500,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        ledger = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9, 0),
                    broker="ledger",
                    type="transfer",
                    symbol="ETH",
                    name="ETH",
                    quantity=1,
                    price=2000,
                    amount=0,
                    currency="USD",
                    original_currency="USD",
                )
            ]
        )
        degiro_book = build_portfolio(degiro, {"IE00B3RBWM25": 110}, {"IE00B3RBWM25": {"source": "live", "as_of": "2024-01-01"}})
        bitvavo_book = build_portfolio(bitvavo, {"BTC": 52000}, {"BTC": {"source": "live", "as_of": "2024-01-01"}})
        ledger_book = build_portfolio(ledger, {"ETH": 2100}, {"ETH": {"source": "live", "as_of": "2024-01-01"}})
        self.assertIn("invested", degiro_book.history.columns)
        self.assertAlmostEqual(float(degiro_book.history.iloc[-1]["invested"]), 100)
        self.assertAlmostEqual(float(degiro_book.history.iloc[-1]["etf_invested"]), 100)
        self.assertAlmostEqual(float(bitvavo_book.history.iloc[-1]["deposits"]), 500)
        self.assertAlmostEqual(float(bitvavo_book.history.iloc[-1]["crypto_invested"]), 500)
        self.assertIn("invested", ledger_book.history.columns)

    def test_degiro_account_and_transactions_merge_the_same_trade_once(self) -> None:
        result = parse_uploads(
            [
                ("Transactions.csv", DEGIRO_DUPLICATE_BUY_TRADES),
                ("Account.csv", DEGIRO_DUPLICATE_BUY_ACCOUNT),
            ]
        )
        self.assertEqual(result.warnings, [])
        rows = result.transactions.loc[result.transactions["type"].eq("buy")]
        self.assertEqual(len(rows), 1)
        trade = rows.iloc[0]
        self.assertAlmostEqual(trade["quantity"], 10)
        self.assertAlmostEqual(trade["price"], 100)
        self.assertAlmostEqual(trade["fees"], 1)
        self.assertAlmostEqual(trade["amount"], -1001)
        self.assertEqual(trade["venue"], "EAM")

    def test_degiro_venue_balances_keep_open_quantity_per_execution_venue(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9, 0),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VUSA",
                    quantity=10,
                    price=100,
                    amount=-1000,
                    currency="EUR",
                    original_currency="EUR",
                    venue="EAM",
                ),
                make_row(
                    date=datetime(2024, 1, 2, 9, 0),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VUSA",
                    quantity=5,
                    price=102,
                    amount=-510,
                    currency="EUR",
                    original_currency="EUR",
                    venue="XET",
                ),
                make_row(
                    date=datetime(2024, 1, 3, 9, 0),
                    broker="degiro",
                    type="sell",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VUSA",
                    quantity=4,
                    price=105,
                    amount=420,
                    currency="EUR",
                    original_currency="EUR",
                    venue="EAM",
                ),
            ]
        )
        balances, warnings = degiro_venue_balances(finalize(rows))
        self.assertEqual(warnings, [])
        self.assertEqual(len(balances), 2)
        eam = balances.loc[balances["venue"].eq("EAM")].iloc[0]
        xet = balances.loc[balances["venue"].eq("XET")].iloc[0]
        self.assertAlmostEqual(float(eam["quantity"]), 6)
        self.assertAlmostEqual(float(xet["quantity"]), 5)

    def test_fee_rate_ignores_trade_rows_without_cash_amount(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9, 0),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VUSA",
                    quantity=3,
                    price=90.353,
                    amount=-272.06,
                    fees=1,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 1, 9, 1),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VUSA",
                    quantity=3,
                    price=90.353,
                    amount=math.nan,
                    fees=0,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        merged = finalize(rows)
        trades = merged.loc[merged["type"].isin(["buy", "sell"])].copy()
        traded = pd.to_numeric(trades["amount"], errors="coerce").abs().dropna()
        self.assertAlmostEqual(float(trades["fees"].sum()), 1)
        self.assertAlmostEqual(float(traded.sum()), 272.06)

    def test_same_degiro_order_one_minute_apart_is_one_purchase(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 8, 5, 9, 4),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VANGUARD S&P 500 UCITS ETF USD DIS",
                    quantity=3,
                    price=90.353,
                    amount=-272.06,
                    fees=1,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 8, 5, 9, 5),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="VANGUARD S&P 500 UCITS ETF USD DIS",
                    quantity=3,
                    price=90.353,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        merged = finalize(rows)
        buys = merged.loc[merged["type"].eq("buy")]
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(buys.iloc[0]["quantity"], 3)
        self.assertAlmostEqual(buys.iloc[0]["amount"], -272.06)
        book = build_portfolio(merged, latest_prices(merged))
        self.assertAlmostEqual(book.holdings.iloc[0]["quantity"], 3)

    def test_two_paid_buys_one_minute_apart_both_count(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 8, 5, 9, 4),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="Vanguard",
                    quantity=3,
                    price=90.353,
                    amount=-272.06,
                    fees=1,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 8, 5, 9, 5),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3XXRP09",
                    isin="IE00B3XXRP09",
                    name="Vanguard",
                    quantity=3,
                    price=90.353,
                    amount=-272.06,
                    fees=1,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        merged = finalize(rows)
        self.assertEqual(len(merged.loc[merged["type"].eq("buy")]), 2)

    def test_degiro_account_deposits_count_once_beside_the_same_purchase(self) -> None:
        account = """Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie EUR,Saldo EUR,Order Id
01-02-2024,08:00,01-02-2024,,,iDEAL storting,,1000.00,1000.00,
01-02-2024,08:00,01-02-2024,,,iDEAL storting,,1000.00,1000.00,
02-02-2024,10:00,02-02-2024,Vanguard FTSE All-World,IE00B3RBWM25,Koop 10 @ 100,00 EUR,,-1001.00,-1.00,99
""".encode()
        result = parse_uploads(
            [
                ("Transactions.csv", DEGIRO_DUPLICATE_BUY_TRADES),
                ("Account.csv", account),
            ]
        )
        rows = result.transactions.sort_values(["date", "type"])
        self.assertEqual(rows["type"].tolist(), ["deposit", "buy"])
        self.assertAlmostEqual(rows.iloc[0]["amount"], 1000)
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertAlmostEqual(book.contributions, 1000)
        self.assertEqual(len(book.holdings), 1)
        self.assertAlmostEqual(book.holdings.iloc[0]["quantity"], 10)
        self.assertAlmostEqual(book.positions["cost"].sum(), 1001)
        self.assertAlmostEqual(book.invested, 1001)

    def test_degiro_account_swapped_mutatie_keeps_dividend_and_cash_sweep_transfer(self) -> None:
        result = parse_uploads([("Account.csv", DEGIRO_ACCOUNT_SWAPPED)])
        self.assertEqual(result.warnings, [])
        kinds = result.transactions.sort_values(["date", "type"])["type"].tolist()
        self.assertIn("dividend", kinds)
        self.assertIn("transfer", kinds)
        transfer = result.transactions.loc[result.transactions["name"].eq("Degiro Cash Sweep Transfer")].iloc[0]
        self.assertEqual(transfer["type"], "transfer")
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertAlmostEqual(book.income, 12.5)
        self.assertAlmostEqual(book.fees, -4.88)
        self.assertAlmostEqual(book.result, 7.62)

    def test_fee_only_duplicate_buy_merges_and_embedded_fee_row_drops(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 2, 2, 10, 0),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3RBWM25",
                    isin="IE00B3RBWM25",
                    name="Vanguard FTSE All-World",
                    quantity=4,
                    price=129.43,
                    amount=-517.72,
                    fees=0,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 2, 2, 10, 1),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3RBWM25",
                    isin="IE00B3RBWM25",
                    name="Vanguard FTSE All-World",
                    quantity=4,
                    price=129.43,
                    amount=-520.72,
                    fees=3,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 2, 2, 10, 0),
                    broker="degiro",
                    type="fee",
                    symbol="EUR",
                    isin="",
                    name="Vanguard FTSE All-World",
                    amount=-3,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        merged = finalize(rows)
        buys = merged.loc[merged["type"].eq("buy")]
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(float(buys.iloc[0]["amount"]), -520.72)
        self.assertEqual(len(merged.loc[merged["type"].eq("fee")]), 0)

    def test_opening_balance_fills_cash_before_the_export_window(self) -> None:
        result = parse_uploads([("Account.csv", PARTIAL_ACCOUNT)])
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertAlmostEqual(book.cash_eur, 1019)
        self.assertAlmostEqual(book.contributions, 999)
        self.assertAlmostEqual(book.income, 20)
        self.assertAlmostEqual(book.result, 20)

    def test_transfer_between_bitvavo_and_ledger_is_counted_once(self) -> None:
        result = parse_uploads(
            [
                ("bitvavo.csv", BITVAVO),
                ("ledger-live.csv", LEDGER),
            ]
        )
        self.assertTrue(any("Skipped 1" in warning for warning in result.warnings))
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertEqual(len(book.holdings), 1)
        holding = book.holdings.iloc[0]
        self.assertEqual(holding["symbol"], "BTC")
        self.assertEqual(holding["security_type"], "Crypto")
        self.assertEqual(holding["country"], "Global")
        self.assertEqual(book.positions.iloc[0]["broker"], "ledger")
        self.assertAlmostEqual(holding["quantity"], 1)
        self.assertAlmostEqual(holding["avg_cost"], 50000)
        self.assertAlmostEqual(holding["current_value"], 60000)
        self.assertAlmostEqual(book.result, 10000)
        self.assertAlmostEqual(book.cash_eur, 0)
        self.assertAlmostEqual(book.contributions, 50000)
        self.assertAlmostEqual(book.net_worth, 60000)
        self.assertAlmostEqual(book.net_worth - book.contributions, book.result)

        ledger_only = result.transactions.loc[result.transactions["broker"].eq("ledger")]
        isolated = build_portfolio(ledger_only)
        hinted = build_portfolio(ledger_only, latest_prices(result.transactions))
        self.assertAlmostEqual(isolated.holdings.iloc[0]["current_value"], 50000)
        self.assertAlmostEqual(hinted.holdings.iloc[0]["current_value"], 60000)
        self.assertAlmostEqual(hinted.unrealized, 10000)
        self.assertEqual(hinted.holdings.iloc[0]["security_type"], "Crypto")
        self.assertEqual(hinted.holdings.iloc[0]["country"], "Global")

    def test_ledger_receipt_before_bitvavo_send_keeps_euro_cost(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1, 9),
                    broker="bitvavo",
                    type="buy",
                    symbol="BTC",
                    quantity=1,
                    price=50000,
                    amount=-50000,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 2, 10),
                    broker="ledger",
                    type="transfer",
                    symbol="BTC",
                    quantity=0.999,
                    price=60000,
                    amount=0,
                    currency="USD",
                    original_currency="USD",
                ),
                make_row(
                    date=datetime(2024, 1, 2, 11),
                    broker="bitvavo",
                    type="transfer",
                    symbol="BTC",
                    quantity=-1,
                    amount=0,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        book = build_portfolio(rows, latest_prices(rows))
        ledger = book.positions.loc[book.positions["broker"].eq("ledger")].iloc[0]
        self.assertAlmostEqual(ledger["quantity"], 0.999)
        self.assertAlmostEqual(ledger["cost"], 50000, places=2)
        self.assertAlmostEqual(float(book.holdings.iloc[0]["quantity"]), 0.999)

    def test_unpriced_holding_stays_blank_instead_of_zero(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2026, 10, 1),
                    broker="bitvavo",
                    type="buy",
                    symbol="LUNA",
                    quantity=10,
                    price=2.5,
                    amount=-25,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        book = build_portfolio(rows, {}, {"LUNA": {"source": "unpriced", "as_of": ""}})
        self.assertTrue(pd.isna(book.holdings.iloc[0]["current_value"]))

    def test_partial_country_weights_keep_the_missing_slice(self) -> None:
        import tempfile

        payload = {
            "IE00TEST0001": {
                "source": "demo",
                "as_of": "2026-10-04",
                "ticker": "TEST",
                "countries": {"United States": 0.6},
                "sectors": {"Technology": 80, "Other": 20},
            }
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "comp.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_etf_composition(path)
        countries = loaded["IE00TEST0001"]["countries"]
        self.assertAlmostEqual(countries["United States"], 0.6)
        self.assertAlmostEqual(countries["Rest of fund"], 0.4)

    def test_bitvavo_deposit_is_crypto_capital_even_when_ledger_countervalue_differs(self) -> None:
        result = parse_uploads(
            [
                ("bitvavo.csv", BITVAVO),
                ("ledger-live.csv", LEDGER_COUNTERVALUE_DRIFT),
            ]
        )
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertEqual(len(book.holdings), 1)
        holding = book.holdings.iloc[0]
        self.assertAlmostEqual(book.contributions, 50000)
        self.assertAlmostEqual(book.cash_eur, 0)
        self.assertAlmostEqual(holding["quantity"], 1)
        self.assertAlmostEqual(holding["avg_cost"], 50000)
        self.assertAlmostEqual(holding["current_value"], 60000)

        ledger_only = result.transactions.loc[result.transactions["broker"].eq("ledger")]
        isolated = build_portfolio(ledger_only)
        self.assertAlmostEqual(isolated.holdings.iloc[0]["avg_cost"], 70000)

    def test_ledger_maps_in_out_reward_and_keeps_the_operation_date_value(self) -> None:
        csv = """Operation Date,Status,Currency Ticker,Currency Name,Operation Type,Operation Amount,Operation Fees,Countervalue Ticker,Countervalue at Operation Date,Countervalue at CSV Export
2024-07-01T10:00:00Z,Confirmed,ETH,Ethereum,IN,2,0,EUR,4000,5000
2024-07-02T10:00:00Z,Confirmed,ETH,Ethereum,OUT,0.5,0.01,EUR,1000,1200
2024-07-03T10:00:00Z,Confirmed,ETH,Ethereum,REWARD,0.1,0,EUR,200,250
2024-07-04T10:00:00Z,Failed,ETH,Ethereum,IN,9,0,EUR,18000,18000
2024-07-05T10:00:00Z,Confirmed,ETH,Ethereum,DELEGATE,2,0,EUR,4000,4000
""".encode()
        result = parse_uploads([("ledger-wallet.csv", csv)])
        self.assertTrue(any("Skipped 1" in warning for warning in result.warnings))
        rows = result.transactions.sort_values("date")
        self.assertEqual(rows["type"].tolist(), ["transfer", "transfer", "transfer"])
        self.assertEqual(rows["name"].tolist(), ["Ethereum", "Ethereum", "Ethereum"])
        inbound, outbound, reward = rows.itertuples(index=False)
        self.assertAlmostEqual(inbound.quantity, 2)
        self.assertAlmostEqual(inbound.price, 2000)
        self.assertEqual(inbound.original_currency, "EUR")
        self.assertAlmostEqual(outbound.quantity, -0.51)
        self.assertAlmostEqual(outbound.fees, 0.01)
        self.assertAlmostEqual(reward.quantity, 0.1)
        self.assertTrue(math.isnan(reward.price))
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        holding = book.holdings.iloc[0]
        self.assertEqual(holding["symbol"], "ETH")
        self.assertEqual(holding["security_type"], "Crypto")
        self.assertEqual(holding["country"], "Global")
        self.assertAlmostEqual(holding["quantity"], 1.59)
        self.assertAlmostEqual(holding["avg_cost"], 2980 / 1.59)

    def test_bitvavo_fee_is_added_only_when_it_is_not_already_in_the_paid_amount(self) -> None:
        result = parse_uploads([("bitvavo.csv", BITVAVO_FEE)])
        paid = result.transactions.sort_values("date")["amount"].tolist()
        self.assertEqual(paid, [-201.0, -101.0])
        trade = result.transactions.sort_values("date").iloc[0]
        self.assertEqual(trade["symbol"], "ETH")
        self.assertEqual(trade["original_currency"], "EUR")
        self.assertEqual(trade["currency"], "EUR")
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        self.assertEqual(book.holdings.iloc[0]["security_type"], "Crypto")

    def test_bitvavo_maps_cash_staking_and_swapped_market(self) -> None:
        csv = """Date,Time,Type,Currency,Amount,Quote Currency,Quote Price,Received / Paid Currency,Received / Paid Amount,Fee currency,Fee amount,Status,Market
2024-06-01,09:00,deposit,EUR,250,,,,,,,Completed,
2024-06-02,09:00,staking reward,ADA,4,,,,,,,Completed,
2024-06-03,09:00,buy,,0.01,,,EUR,500,EUR,0,Completed,BTC-EUR
2024-06-04,09:00,withdrawal,EUR,25,,,,,,,Completed,
""".encode()
        result = parse_uploads([("bitvavo.csv", csv)])
        rows = result.transactions.sort_values("date")
        self.assertEqual(rows["type"].tolist(), ["deposit", "transfer", "buy", "withdrawal"])
        self.assertEqual(rows["symbol"].tolist(), ["EUR", "ADA", "BTC", "EUR"])
        buy = rows.iloc[2]
        self.assertEqual(buy["original_currency"], "EUR")
        self.assertAlmostEqual(buy["quantity"], 0.01)
        self.assertAlmostEqual(buy["amount"], -500)
        book = build_portfolio(result.transactions, latest_prices(result.transactions))
        ada = book.holdings.loc[book.holdings["symbol"].eq("ADA")].iloc[0]
        self.assertAlmostEqual(ada["quantity"], 4)
        self.assertEqual(ada["security_type"], "Crypto")
        self.assertAlmostEqual(ada["avg_cost"], 0)

    def test_normalize_joins_the_three_books_and_splits_cash(self) -> None:
        degiro = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="deposit",
                    symbol="EUR",
                    name="EUR",
                    amount=1000,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 2),
                    broker="degiro",
                    type="buy",
                    symbol="IE00B3RBWM25",
                    isin="IE00B3RBWM25",
                    name="VWRL",
                    quantity=10,
                    price=80,
                    amount=-800,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        bitvavo = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 2, 1),
                    broker="bitvavo",
                    type="buy",
                    symbol="XBT",
                    name="XBT",
                    quantity=1,
                    price=50000,
                    amount=-50000,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        ledger = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 3, 1),
                    broker="ledger",
                    type="transfer",
                    symbol="ETH",
                    name="ETH",
                    quantity=2,
                    price=2000,
                    amount=0,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        transactions, holdings, cash = normalize(degiro, bitvavo, ledger)
        self.assertEqual(transactions["broker"].tolist(), ["degiro", "degiro", "bitvavo", "ledger"])
        self.assertEqual(transactions.loc[transactions["broker"].eq("bitvavo"), "symbol"].iloc[0], "BTC")
        self.assertEqual(set(holdings["symbol"]), {"IE00B3RBWM25", "BTC", "ETH"})
        self.assertNotIn("EUR", set(holdings["symbol"]))
        equity = holdings.loc[holdings["symbol"].eq("IE00B3RBWM25")].iloc[0]
        self.assertAlmostEqual(equity["quantity"], 10)
        self.assertAlmostEqual(equity["avg_cost"], 80)
        coin = holdings.loc[holdings["symbol"].eq("ETH")].iloc[0]
        self.assertEqual(coin["security_type"], "Crypto")
        self.assertEqual(coin["country"], "Global")
        self.assertAlmostEqual(coin["avg_cost"], 2000)
        balances = cash.set_index("broker")["balance"]
        self.assertAlmostEqual(balances["degiro"], 200)
        self.assertAlmostEqual(balances["bitvavo"], -50000)
        self.assertNotIn("ledger", balances.index)

    def test_unknown_file_is_reported(self) -> None:
        result = parse_uploads([("notes.csv", b"hello,world\n1,2\n")])
        self.assertEqual(len(result.transactions), 0)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("not a DEGIRO, Bitvavo, or Ledger export", result.warnings[0])
        empty = parse_uploads([("blank.csv", b"   \n")])
        self.assertIn("blank.csv is empty", empty.warnings[0])

    def test_overrides_json_replaces_sector_and_country(self) -> None:
        import tempfile

        import pandas as pd

        from processing.enrich import apply_user_overrides

        holdings = pd.DataFrame(
            [
                {
                    "symbol": "VWRL",
                    "isin": "IE00B3RBWM25",
                    "name": "Vanguard",
                    "quantity": 1,
                    "avg_cost": 1,
                    "current_value": 1,
                    "currency": "EUR",
                    "sector": "",
                    "industry": "",
                    "country": "",
                    "exchange": "",
                    "security_type": "etf",
                }
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            override_path = Path(folder) / "overrides.json"
            override_path.write_text(
                '{"IE00B3RBWM25": {"sector": "Diversified", "country": "Ireland"}}',
                encoding="utf-8",
            )
            updated, error = apply_user_overrides(holdings, override_path)
            broken = Path(folder) / "broken.json"
            broken.write_text("{", encoding="utf-8")
            _, message = apply_user_overrides(holdings, broken)
        self.assertIsNone(error)
        self.assertEqual(updated.iloc[0]["sector"], "Diversified")
        self.assertEqual(updated.iloc[0]["country"], "Ireland")
        self.assertIn("not valid JSON", message)

    def test_cache_roundtrip(self) -> None:
        import tempfile
        from unittest.mock import patch

        result = parse_uploads([("Account.csv", DEGIRO_ACCOUNT)])
        with tempfile.TemporaryDirectory() as folder:
            with patch("processing.normalize.DATA_DIR", Path(folder)):
                save_cache(result.transactions, {"imported_at": "2026-10-02T12:00", "files": []})
                loaded, meta = load_cache()
        self.assertIsNotNone(loaded)
        self.assertTrue(meta.get("sqlite"))
        self.assertEqual(len(loaded), len(result.transactions))
        reloaded = build_portfolio(loaded, latest_prices(loaded))
        self.assertAlmostEqual(reloaded.result, 19)

    def test_price_hints_pick_preferred_eur_ticker_and_convert_fx(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="AAA",
                    isin="ISIN_EUR",
                    name="AAA",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="BBB",
                    isin="ISIN_USD",
                    name="BBB",
                    quantity=1,
                    price=80,
                    amount=-80,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        searches: list[str] = []

        class FakeTicker:
            def __init__(self, symbol: str) -> None:
                table = {
                    "AAA.EU": {"last_price": 130.0, "currency": "EUR"},
                    "AAA.US": {"last_price": 130.0, "currency": "USD"},
                    "BBB.US": {"last_price": 50.0, "currency": "USD"},
                    "USDEUR=X": {"last_price": 0.9, "currency": "EUR"},
                }
                quote = table.get(symbol, {"last_price": None, "currency": ""})
                self.fast_info = quote
                self.info = {"currency": quote.get("currency")}
                self.symbol = symbol

        def fake_search(query: str, max_results: int = 8):
            del max_results
            searches.append(query)
            if query == "ISIN_EUR":
                return types.SimpleNamespace(
                    quotes=[
                        {"symbol": "AAA.US", "quoteType": "ETF", "currency": "USD", "exchange": "NYSE"},
                        {"symbol": "AAA.EU", "quoteType": "ETF", "currency": "EUR", "exchange": "EAM"},
                    ]
                )
            if query == "ISIN_USD":
                return types.SimpleNamespace(
                    quotes=[{"symbol": "BBB.US", "quoteType": "EQUITY", "currency": "USD", "exchange": "NYSE"}]
                )
            return types.SimpleNamespace(quotes=[])

        fake_yf = types.SimpleNamespace(Search=fake_search, Ticker=lambda symbol: FakeTicker(symbol))
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            with patch.dict(sys.modules, {"yfinance": fake_yf}):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
                )
            self.assertEqual(searches, ["ISIN_EUR", "ISIN_USD"])
            self.assertAlmostEqual(hints["ISIN_EUR"], 130.0)
            self.assertAlmostEqual(hints["ISIN_USD"], 45.0)
            self.assertEqual(sources["ISIN_EUR"], "live")
            self.assertEqual(sources["ISIN_USD"], "live")
            payload = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(payload["ISIN_EUR"]["ticker"], "AAA.EU")
            self.assertEqual(payload["ISIN_EUR"]["quote_currency"], "EUR")
            self.assertEqual(payload["ISIN_EUR"]["source"], "search")
            self.assertEqual(payload["ISIN_USD"]["ticker"], "BBB.US")
            self.assertEqual(payload["ISIN_USD"]["quote_currency"], "USD")
            self.assertIn("as_of", payload["ISIN_USD"])

    def test_price_hints_fallback_cache_then_csv(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="VWRL",
                    isin="IE00B3RBWM25",
                    name="Vanguard",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        broken_yf = types.SimpleNamespace(
            Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            Ticker=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            cache.write_text(
                json.dumps(
                    {
                        "IE00B3RBWM25": {
                            "isin_or_key": "IE00B3RBWM25",
                            "ticker": "VWCE.AS",
                            "quote_currency": "EUR",
                            "exchange": "EAM",
                            "price_eur": 111.0,
                            "as_of": "2026-09-20T08:00:00+00:00",
                            "source": "search",
                            "confidence": "high",
                            "mapped_at": "2026-09-20T08:00:00+00:00",
                            "failure_count": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )
            broken_yf = types.SimpleNamespace(
                Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
                Ticker=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            )
            with (
                patch.dict(sys.modules, {"yfinance": broken_yf}),
                patch("processing.enrich._justetf_eur_quote", return_value=None),
                patch("processing.enrich._yahoo_chart_quote", return_value=None),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
                )
            self.assertAlmostEqual(hints["IE00B3RBWM25"], 111.0)
            self.assertEqual(sources["IE00B3RBWM25"], "cache")

            empty_cache = Path(folder) / "fresh.json"
            with (
                patch.dict(sys.modules, {"yfinance": broken_yf}),
                patch("processing.enrich._justetf_eur_quote", return_value=None),
                patch("processing.enrich._yahoo_chart_quote", return_value=None),
            ):
                fallback, fallback_sources = price_hints_with_sources(
                    frame,
                    cache_path=empty_cache,
                    now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
                )
            self.assertAlmostEqual(fallback["IE00B3RBWM25"], 100.0)
            self.assertEqual(fallback_sources["IE00B3RBWM25"], "csv")

    def test_price_cache_with_nan_keeps_valid_rows(self) -> None:
        import tempfile
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="VWCE",
                    isin="IE00BK5BQT80",
                    name="Vanguard",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            cache.write_text(
                """
{
  "IE00BK5BQT80": {"price_eur": 123.45, "as_of": "2026-10-03T08:00:00+00:00"},
  "BROKEN": {"price_eur": NaN, "as_of": ""}
}
                """.strip(),
                encoding="utf-8",
            )
            with (
                patch("processing.enrich._justetf_eur_quote", return_value=None),
                patch("processing.enrich._yahoo_chart_quote", return_value=None),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
                )
        self.assertAlmostEqual(hints["IE00BK5BQT80"], 123.45)
        self.assertEqual(sources["IE00BK5BQT80"], "cache")

    def test_etf_target_ticker_is_used_without_search(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="IE00BK5BQT80",
                    isin="IE00BK5BQT80",
                    name="Vanguard",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )

        class FakeTicker:
            def __init__(self, symbol: str) -> None:
                self.fast_info = {"last_price": 150.0, "currency": "EUR"} if symbol == "VWCE.AS" else {}
                self.info = {}

        fake_yf = types.SimpleNamespace(
            Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("search should not run")),
            Ticker=FakeTicker,
        )
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            with patch.dict(sys.modules, {"yfinance": fake_yf}):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
                )
        self.assertAlmostEqual(hints["IE00BK5BQT80"], 150.0)
        self.assertEqual(sources["IE00BK5BQT80"], "live")

    def test_crypto_prices_come_from_bitvavo_not_yahoo(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="bitvavo",
                    type="buy",
                    symbol="BTC",
                    name="Bitcoin",
                    quantity=0.01,
                    price=50000,
                    amount=-500,
                    currency="EUR",
                    original_currency="EUR",
                ),
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="VWRL",
                    isin="IE00B3RBWM25",
                    name="Vanguard",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                ),
            ]
        )
        yahoo_queries: list[str] = []

        def fake_search(query: str, max_results: int = 8):
            del max_results
            yahoo_queries.append(query)
            if query == "IE00B3RBWM25":
                return types.SimpleNamespace(
                    quotes=[{"symbol": "VWCE.AS", "quoteType": "ETF", "currency": "EUR", "exchange": "EAM"}]
                )
            if query == "BTC":
                return types.SimpleNamespace(
                    quotes=[{"symbol": "BTC-USD", "quoteType": "CRYPTOCURRENCY", "currency": "USD", "exchange": "CCC"}]
                )
            return types.SimpleNamespace(quotes=[])

        class FakeTicker:
            def __init__(self, symbol: str) -> None:
                self.fast_info = {"last_price": 120.0, "currency": "EUR"} if symbol in {"VWCE.AS", "VWRL.AS"} else {}
                self.info = {}

        fake_yf = types.SimpleNamespace(Search=fake_search, Ticker=FakeTicker)
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            with (
                patch.dict(sys.modules, {"yfinance": fake_yf}),
                patch("processing.enrich._bitvavo_eur_prices", return_value={"BTC": 90000.0}),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
                )
            self.assertEqual(yahoo_queries, [])
            self.assertAlmostEqual(hints["BTC"], 90000.0)
            self.assertAlmostEqual(hints["IE00B3RBWM25"], 120.0)
            self.assertEqual(sources["BTC"], "live")
            stored = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(stored["BTC"]["source"], "bitvavo")
            self.assertEqual(stored["BTC"]["ticker"], "BTC-EUR")

    def test_dividend_padi_history_cagr_and_projection(self) -> None:
        rows = [
            make_row(
                date=datetime(2020, 1, 1),
                broker="degiro",
                type="buy",
                symbol="DIV",
                isin="IE00DIV00001",
                name="Dividend Co",
                quantity=10,
                price=100,
                amount=-1000,
                currency="EUR",
                original_currency="EUR",
            )
        ]
        cash = [10, 11, 12.1, 13.31, 14.641]
        for year, amount in zip(range(2020, 2025), cash):
            rows.append(
                make_row(
                    date=datetime(year, 6, 1),
                    broker="degiro",
                    type="dividend",
                    symbol="DIV",
                    isin="IE00DIV00001",
                    name="Dividend Co",
                    amount=amount,
                    currency="EUR",
                    original_currency="EUR",
                )
            )
        rows.append(
            make_row(
                date=datetime(2024, 7, 1),
                broker="degiro",
                type="buy",
                symbol="DIV",
                isin="IE00DIV00001",
                name="Dividend Co",
                quantity=10,
                price=100,
                amount=-1000,
                currency="EUR",
                original_currency="EUR",
            )
        )
        book = build_portfolio(records_to_frame(rows))
        self.assertAlmostEqual(book.padi, 20 * (14.641 / 10))
        history = dividend_history(book.dividends)
        self.assertEqual(history["month"].loc[history["month"]["period"].eq("2024-06"), "amount"].iloc[0], 14.641)
        self.assertEqual(history["quarter"].loc[history["quarter"]["period"].eq("2024Q2"), "amount"].iloc[0], 14.641)
        self.assertEqual(history["year"]["period"].tolist(), ["2020", "2021", "2022", "2023", "2024"])
        growth = dividend_cagr(book.dividends).set_index("symbol")
        self.assertAlmostEqual(growth.loc["DIV", "cagr_1y"], 0.1)
        self.assertAlmostEqual(growth.loc["DIV", "cagr_3y"], 0.1)
        self.assertTrue(math.isnan(growth.loc["DIV", "cagr_5y"]))
        self.assertAlmostEqual(growth.loc["Portfolio", "cagr_1y"], 0.1)
        supplied = book.holdings.copy()
        supplied["expected_annual_dividend"] = 2
        self.assertAlmostEqual(current_padi(supplied), 40)
        kept = project_padi(1000, 100, 0.04, 1, reinvest=False).iloc[-1]
        self.assertAlmostEqual(kept["contribution"], 1200)
        self.assertAlmostEqual(kept["reinvested"], 0)
        self.assertAlmostEqual(kept["padi"], 1000 * 1.04 + 1200 * 0.04)
        snowball = project_padi(1000, 100, 0.04, 1, reinvest=True).iloc[-1]
        self.assertAlmostEqual(snowball["reinvested"], 1000)
        self.assertAlmostEqual(snowball["padi"], 1000 * 1.04 + 2200 * 0.04)

    def test_enrich_holdings_uses_the_cache_for_symbols_it_has_already_seen(self) -> None:
        import tempfile
        from unittest.mock import patch

        import pandas as pd

        from processing.enrich import enrich_holdings

        def holding(symbol: str, security_type: str = "equity") -> dict[str, object]:
            row = {column: "" for column in HOLDING_COLUMNS}
            row.update(
                symbol=symbol,
                name=symbol,
                quantity=1,
                avg_cost=1,
                current_value=1,
                currency="EUR",
                security_type=security_type,
            )
            return row

        def blank() -> dict[str, str]:
            return {field: "" for field in ("sector", "industry", "country", "exchange", "security_type", "currency")}

        finance = {
            "AAPL": {
                "sector": "Technology",
                "industry": "Consumer Electronics",
                "country": "United States",
                "exchange": "NASDAQ",
                "security_type": "equity",
                "currency": "USD",
            }
        }
        yahoo = {
            "MSFT": {
                "sector": "Technology",
                "industry": "Software",
                "country": "United States",
                "exchange": "NASDAQ",
                "security_type": "equity",
                "currency": "USD",
            }
        }
        calls: list[tuple[str, str]] = []

        def from_finance(symbol: str, isin: str) -> dict[str, str]:
            del isin
            calls.append(("finance", symbol))
            return finance.get(symbol, blank())

        def from_yahoo(symbol: str, isin: str) -> dict[str, str]:
            del isin
            calls.append(("yahoo", symbol))
            return yahoo.get(symbol, blank())

        frame = pd.DataFrame([holding("AAPL"), holding("MSFT"), holding("BTC", "Crypto")])
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "instruments.json"
            with patch("processing.enrich._from_finance_database", side_effect=from_finance), patch(
                "processing.enrich._from_yfinance", side_effect=from_yahoo
            ):
                first = enrich_holdings(frame, cache_path=cache)
                seen_first_pass = list(calls)
                enrich_holdings(frame, cache_path=cache)
            self.assertEqual(
                seen_first_pass,
                [("finance", "AAPL"), ("finance", "MSFT"), ("yahoo", "MSFT"), ("finance", "BTC"), ("yahoo", "BTC")],
            )
            self.assertEqual(calls, seen_first_pass + [("finance", "BTC"), ("yahoo", "BTC")])
            apple = first.loc[first["symbol"].eq("AAPL")].iloc[0]
            microsoft = first.loc[first["symbol"].eq("MSFT")].iloc[0]
            bitcoin = first.loc[first["symbol"].eq("BTC")].iloc[0]
            self.assertEqual(apple["sector"], "Technology")
            self.assertEqual(apple["industry"], "Consumer Electronics")
            self.assertEqual(apple["country"], "United States")
            self.assertEqual(apple["exchange"], "NASDAQ")
            self.assertEqual(apple["currency"], "EUR")
            self.assertEqual(microsoft["industry"], "Software")
            self.assertEqual(bitcoin["sector"], "Crypto")
            self.assertEqual(bitcoin["industry"], "Bitcoin")
            self.assertEqual(bitcoin["country"], "Global")
            self.assertEqual(bitcoin["security_type"], "Crypto")
            stored = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(set(stored), {"AAPL", "MSFT"})

    def test_enrich_holdings_retries_blank_cached_isin_and_updates_profile(self) -> None:
        import tempfile
        from unittest.mock import patch

        import pandas as pd

        from processing.enrich import enrich_holdings

        isin = "IE00TEST0001"
        frame = pd.DataFrame(
            [
                {
                    "symbol": "VWCE",
                    "isin": isin,
                    "name": "Vanguard",
                    "quantity": 1.0,
                    "avg_cost": 100.0,
                    "current_value": 100.0,
                    "currency": "EUR",
                    "sector": "",
                    "industry": "",
                    "country": "",
                    "exchange": "",
                    "security_type": "equity",
                }
            ]
        )
        blank = {field: "" for field in ("sector", "industry", "country", "exchange", "security_type", "currency")}
        filled = {
            "sector": "Diversified",
            "industry": "World equity",
            "country": "Ireland",
            "exchange": "Euronext Amsterdam",
            "security_type": "etf",
            "currency": "EUR",
        }
        calls: list[tuple[str, str]] = []

        def from_finance(symbol: str, given_isin: str) -> dict[str, str]:
            calls.append((symbol, given_isin))
            if given_isin == isin:
                return filled
            return blank

        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "instruments.json"
            cache.write_text(json.dumps({isin: blank}), encoding="utf-8")
            with patch("processing.enrich._from_finance_database", side_effect=from_finance), patch(
                "processing.enrich._from_yfinance", return_value=blank
            ):
                first = enrich_holdings(frame, cache_path=cache)
                seen = list(calls)
                enrich_holdings(frame, cache_path=cache)
            self.assertEqual(calls, seen)
            row = first.iloc[0]
            self.assertEqual(row["sector"], "Diversified")
            self.assertEqual(row["industry"], "World equity")
            self.assertEqual(row["country"], "Ireland")
            self.assertEqual(row["exchange"], "Euronext Amsterdam")
            self.assertEqual(row["security_type"], "etf")
            self.assertEqual(row["currency"], "EUR")
            stored = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(stored[isin]["sector"], "Diversified")

    def test_yahoo_etf_category_fills_sector_and_industry(self) -> None:
        from processing.enrich import _profile_from_yahoo

        profile = _profile_from_yahoo(
            {
                "quoteType": "ETF",
                "category": "World equity",
                "country": "Ireland",
                "fullExchangeName": "Euronext Amsterdam",
                "currency": "EUR",
            }
        )
        self.assertEqual(profile["sector"], "World equity")
        self.assertEqual(profile["industry"], "World equity")
        self.assertEqual(profile["security_type"], "etf")

    def test_apply_lookthrough_splits_etf_but_keeps_crypto_as_one_bucket(self) -> None:
        holdings = pd.DataFrame(
            [
                {
                    "symbol": "VWCE",
                    "isin": "IE00BK5BQT80",
                    "name": "FTSE All-World",
                    "quantity": 1,
                    "avg_cost": 100,
                    "current_value": 1000,
                    "currency": "EUR",
                    "sector": "Diversified",
                    "industry": "World equity",
                    "country": "Ireland",
                    "exchange": "Euronext Amsterdam",
                    "security_type": "etf",
                },
                {
                    "symbol": "BTC",
                    "isin": "",
                    "name": "Bitcoin",
                    "quantity": 0.1,
                    "avg_cost": 10000,
                    "current_value": 500,
                    "currency": "EUR",
                    "sector": "Crypto",
                    "industry": "Bitcoin",
                    "country": "Global",
                    "exchange": "",
                    "security_type": "Crypto",
                },
            ]
        )
        composition = {
            "IE00BK5BQT80": {
                "sectors": {
                    "Information technology": 0.5,
                    "Financials": 0.5,
                }
            }
        }
        expanded = apply_lookthrough(holdings, "sector", composition)
        self.assertAlmostEqual(float(expanded["current_value"].sum()), 1500)
        etf = expanded.loc[expanded["isin"].eq("IE00BK5BQT80")]
        self.assertEqual(set(etf["sector"]), {"Information technology", "Financials"})
        self.assertAlmostEqual(float(etf["current_value"].sum()), 1000)
        crypto = expanded.loc[expanded["symbol"].eq("BTC")]
        self.assertEqual(len(crypto), 1)
        self.assertEqual(crypto.iloc[0]["sector"], "Crypto")
        self.assertAlmostEqual(float(crypto.iloc[0]["current_value"]), 500)

    def test_apply_lookthrough_falls_back_when_no_composition_exists(self) -> None:
        holdings = pd.DataFrame(
            [
                {
                    "symbol": "VUSA",
                    "isin": "IE00B3XXRP09",
                    "name": "S&P 500",
                    "quantity": 1,
                    "avg_cost": 100,
                    "current_value": 900,
                    "currency": "EUR",
                    "sector": "Not set",
                    "industry": "Not set",
                    "country": "Not set",
                    "exchange": "Not set",
                    "security_type": "etf",
                }
            ]
        )
        expanded = apply_lookthrough(holdings, "sector", {})
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded.iloc[0]["sector"], "Not set")
        self.assertAlmostEqual(float(expanded.iloc[0]["current_value"]), 900)

    def test_apply_lookthrough_country_splits_etf_and_labels_crypto(self) -> None:
        holdings = pd.DataFrame(
            [
                {
                    "symbol": "VUSA",
                    "isin": "IE00B3XXRP09",
                    "name": "S&P 500",
                    "quantity": 1,
                    "avg_cost": 100,
                    "current_value": 1000,
                    "currency": "EUR",
                    "sector": "Diversified",
                    "industry": "World equity",
                    "country": "Ireland",
                    "exchange": "Euronext Amsterdam",
                    "security_type": "equity",
                },
                {
                    "symbol": "BTC",
                    "isin": "",
                    "name": "Bitcoin",
                    "quantity": 1,
                    "avg_cost": 1000,
                    "current_value": 500,
                    "currency": "EUR",
                    "sector": "Crypto",
                    "industry": "Bitcoin",
                    "country": "Global",
                    "exchange": "",
                    "security_type": "Crypto",
                },
            ]
        )
        composition = {
            "IE00B3XXRP09": {
                "countries": {
                    "United States": 0.9,
                    "Canada": 0.1,
                }
            }
        }
        expanded = apply_lookthrough(holdings, "country", composition)
        etf = expanded.loc[expanded["symbol"].eq("VUSA")]
        self.assertEqual(set(etf["country"]), {"United States", "Canada"})
        self.assertAlmostEqual(float(etf["current_value"].sum()), 1000)
        crypto = expanded.loc[expanded["symbol"].eq("BTC")]
        self.assertEqual(len(crypto), 1)
        self.assertEqual(crypto.iloc[0]["country"], "Crypto")

    def test_apply_lookthrough_currency_comes_from_country_and_crypto_stays_crypto(self) -> None:
        holdings = pd.DataFrame(
            [
                {
                    "symbol": "VWCE",
                    "isin": "IE00BK5BQT80",
                    "name": "FTSE All-World",
                    "quantity": 1,
                    "avg_cost": 100,
                    "current_value": 1200,
                    "currency": "EUR",
                    "sector": "Diversified",
                    "industry": "World equity",
                    "country": "Ireland",
                    "exchange": "Euronext Amsterdam",
                    "security_type": "equity",
                },
                {
                    "symbol": "ETH",
                    "isin": "",
                    "name": "Ethereum",
                    "quantity": 1,
                    "avg_cost": 1000,
                    "current_value": 300,
                    "currency": "EUR",
                    "sector": "Crypto",
                    "industry": "Ethereum",
                    "country": "Global",
                    "exchange": "",
                    "security_type": "Crypto",
                },
            ]
        )
        composition = {
            "IE00BK5BQT80": {
                "countries": {
                    "United States": 0.6,
                    "Japan": 0.2,
                    "France": 0.2,
                }
            }
        }
        expanded = apply_lookthrough(holdings, "currency", composition)
        etf = expanded.loc[expanded["symbol"].eq("VWCE")]
        self.assertEqual(set(etf["currency"]), {"USD", "JPY", "EUR"})
        self.assertAlmostEqual(float(etf["current_value"].sum()), 1200)
        crypto = expanded.loc[expanded["symbol"].eq("ETH")]
        self.assertEqual(len(crypto), 1)
        self.assertEqual(crypto.iloc[0]["currency"], "Crypto")

    def test_apply_lookthrough_currency_keeps_usdc_as_usd(self) -> None:
        holdings = pd.DataFrame(
            [
                {
                    "symbol": "USDC",
                    "isin": "",
                    "name": "USD Coin",
                    "quantity": 100,
                    "avg_cost": 1,
                    "current_value": 95,
                    "currency": "EUR",
                    "sector": "Crypto",
                    "industry": "USDC",
                    "country": "Global",
                    "exchange": "Bitvavo",
                    "security_type": "Crypto",
                }
            ]
        )
        expanded = apply_lookthrough(holdings, "currency", {})
        self.assertEqual(len(expanded), 1)
        self.assertEqual(expanded.iloc[0]["currency"], "USD")

    def test_forced_unpriced_symbols_do_not_get_portfolio_value(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2026, 10, 1),
                    broker="bitvavo",
                    type="buy",
                    symbol="LUNA",
                    quantity=10,
                    price=2.5,
                    amount=-25,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        hints, _sources, details = price_hints_with_details(rows, now=datetime(2026, 10, 4))
        self.assertNotIn("LUNA", hints)
        book = build_portfolio(rows, hints, details, now=datetime(2026, 10, 4))
        self.assertTrue(bool(book.positions.iloc[0]["unpriced"]))
        self.assertTrue(pd.isna(book.positions.iloc[0]["current_value"]))

    def test_load_etf_composition_normalizes_weights(self) -> None:
        import tempfile

        payload = {
            "IE00BK5BQT80": {
                "source": "demo",
                "as_of": "2026-10-03",
                "ticker": "VWCE.AS",
                "countries": {
                    "usa": 50,
                    "uk": 40,
                    "other": 10,
                },
                "sectors": {
                    "Technology": 25,
                    "Financial Services": 25,
                    "HealthCare": 40,
                    "Other": 10,
                },
            }
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "comp.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_etf_composition(path)
        sectors = loaded["IE00BK5BQT80"]["sectors"]
        self.assertAlmostEqual(sum(sectors.values()), 1.0)
        self.assertEqual(set(sectors), {"Information technology", "Financials", "Health care", "Rest of fund"})
        countries = loaded["IE00BK5BQT80"]["countries"]
        self.assertAlmostEqual(sum(countries.values()), 1.0)
        self.assertEqual(set(countries), {"United States", "United Kingdom", "Rest of fund"})

    def test_crypto_live_entry_supports_sonic_alias_for_ftm(self) -> None:
        now = datetime.fromisoformat("2026-10-04T10:55:50+00:00")
        row = _crypto_live_entry("FTM", {"SONIC": 0.42}, now)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["ticker"], "SONIC-EUR")
        self.assertAlmostEqual(float(row["price_eur"]), 0.42)

    def test_refresh_parser_reads_globalx_data_maps(self) -> None:
        from scripts.refresh_etf_composition import _extract_globalx_map

        payload = '{"data":{"Industrials":0.862,"Information Technology":0.138},"title":"Sector"}'
        sectors = _extract_globalx_map(payload, "Sector")
        self.assertAlmostEqual(sectors["Industrials"], 0.862)
        self.assertAlmostEqual(sectors["Information Technology"], 0.138)

    def test_refresh_parser_reads_justetf_country_rows(self) -> None:
        from scripts.refresh_etf_composition import _extract_justetf_rows

        html = """
        <tr data-testid="etf-holdings_countries_row">
          <td data-testid="tl_etf-holdings_countries_value_name">United States</td>
          <td><span data-testid="tl_etf-holdings_countries_value_percentage">94.08%</span></td>
        </tr>
        <tr data-testid="etf-holdings_countries_row">
          <td data-testid="tl_etf-holdings_countries_value_name">Other</td>
          <td><span data-testid="tl_etf-holdings_countries_value_percentage">5.92%</span></td>
        </tr>
        """
        rows = dict(_extract_justetf_rows(html, "countries"))
        self.assertAlmostEqual(rows["United States"], 0.9408)
        self.assertAlmostEqual(rows["Other"], 0.0592)

    def test_old_csv_price_still_counts_toward_gain(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="VWCE",
                    isin="IE00BK5BQT80",
                    name="Vanguard",
                    quantity=40,
                    price=143.13,
                    amount=-5725.2,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        hints = {"IE00BK5BQT80": 166.26}
        details = {"IE00BK5BQT80": {"source": "csv", "as_of": "2026-09-02T09:13:00"}}
        book = build_portfolio(rows, hints, details, now=datetime(2026, 10, 4))
        position = book.positions.iloc[0]
        self.assertFalse(bool(position["unpriced"]))
        self.assertAlmostEqual(position["current_value"], 6650.4)
        self.assertAlmostEqual(position["unrealized"], 925.2)
        self.assertAlmostEqual(book.market_value, 6650.4)

    def test_ftm_price_hint_is_included_in_gain(self) -> None:
        rows = records_to_frame(
            [
                make_row(
                    date=datetime(2026, 10, 1),
                    broker="bitvavo",
                    type="buy",
                    symbol="FTM",
                    quantity=10,
                    price=0.5,
                    amount=-5,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        hints = {"FTM": 0.04}
        details = {"FTM": {"source": "live", "as_of": "2026-10-04T12:00:00+00:00"}}
        book = build_portfolio(rows, hints, details, now=datetime(2026, 10, 4))
        position = book.positions.iloc[0]
        self.assertFalse(bool(position["unpriced"]))
        self.assertAlmostEqual(position["current_value"], 0.4)

    def test_justetf_quote_is_used_when_yahoo_fails(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="VWCE",
                    isin="IE00BK5BQT80",
                    name="Vanguard",
                    quantity=1,
                    price=100,
                    amount=-100,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        broken_yf = types.SimpleNamespace(
            Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            Ticker=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        quote = {
            "quote_currency": "EUR",
            "price_eur": 170.98,
            "as_of": "2026-10-02",
            "source": "justetf",
            "confidence": "eur",
            "mapped_at": "2026-10-04T12:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            with (
                patch.dict(sys.modules, {"yfinance": broken_yf}),
                patch("processing.enrich._justetf_eur_quote", return_value=quote),
                patch("processing.enrich._yahoo_chart_quote", return_value=None),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
                )
        self.assertAlmostEqual(hints["IE00BK5BQT80"], 170.98)
        self.assertEqual(sources["IE00BK5BQT80"], "live")

    def test_yahoo_chart_prices_euro_listing_when_yfinance_is_down(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2024, 1, 1),
                    broker="degiro",
                    type="buy",
                    symbol="VWCE",
                    isin="IE00BK5BQT80",
                    name="Vanguard",
                    quantity=40,
                    price=100,
                    amount=-4000,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        broken_yf = types.SimpleNamespace(
            Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            Ticker=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        chart = {"price": 171.025, "currency": "EUR", "as_of": "2026-10-02T15:25:02+00:00"}
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            with (
                patch.dict(sys.modules, {"yfinance": broken_yf}),
                patch("processing.enrich._yahoo_chart_quote", return_value=chart),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
                )
        self.assertAlmostEqual(hints["IE00BK5BQT80"], 171.025)
        self.assertEqual(sources["IE00BK5BQT80"], "live")

    def test_old_saved_price_is_replaced_on_the_next_lookup(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2026, 1, 2),
                    broker="degiro",
                    type="buy",
                    symbol="VWCE",
                    isin="IE00BK5BQT80",
                    name="Vanguard",
                    quantity=1,
                    price=145,
                    amount=-145,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        broken_yf = types.SimpleNamespace(
            Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            Ticker=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        chart = {"price": 190.5, "currency": "EUR", "as_of": "2027-01-04T16:30:00+00:00"}
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            cache.write_text(
                json.dumps(
                    {
                        "IE00BK5BQT80": {
                            "isin_or_key": "IE00BK5BQT80",
                            "ticker": "VWCE.AS",
                            "quote_currency": "EUR",
                            "price_eur": 171.025,
                            "as_of": "2026-10-02T15:25:02+00:00",
                            "source": "yahoo",
                            "mapped_at": "2026-10-02T15:25:02+00:00",
                            "failure_count": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(sys.modules, {"yfinance": broken_yf}),
                patch("processing.enrich._yahoo_chart_quote", return_value=chart),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2027, 1, 4, 18, 0, tzinfo=timezone.utc),
                )
        self.assertAlmostEqual(hints["IE00BK5BQT80"], 190.5)
        self.assertEqual(sources["IE00BK5BQT80"], "live")

    def test_london_listing_uses_justetf_euro_quote(self) -> None:
        import sys
        import tempfile
        import types
        from datetime import timezone
        from unittest.mock import patch

        frame = records_to_frame(
            [
                make_row(
                    date=datetime(2026, 3, 9),
                    broker="degiro",
                    type="buy",
                    symbol="ARMR",
                    isin="IE000JCW3DZ3",
                    name="Global X Defence",
                    quantity=1,
                    price=30.205,
                    amount=-33.2,
                    currency="EUR",
                    original_currency="EUR",
                )
            ]
        )
        broken_yf = types.SimpleNamespace(
            Search=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
            Ticker=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        chart = {"price": 27.025, "currency": "USD", "as_of": "2026-10-02T15:10:41+00:00"}
        euro = {
            "quote_currency": "EUR",
            "price_eur": 24.02,
            "as_of": "2026-10-02",
            "source": "justetf",
            "confidence": "eur",
            "mapped_at": "2026-10-04T12:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "prices.json"
            with (
                patch.dict(sys.modules, {"yfinance": broken_yf}),
                patch("processing.enrich._yahoo_chart_quote", return_value=chart),
                patch("processing.enrich._justetf_eur_quote", return_value=euro),
            ):
                hints, sources = price_hints_with_sources(
                    frame,
                    cache_path=cache,
                    now=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
                )
        self.assertAlmostEqual(hints["IE000JCW3DZ3"], 24.02)
        self.assertEqual(sources["IE000JCW3DZ3"], "live")

    def test_justetf_load_more_path_points_at_the_expanded_table(self) -> None:
        from scripts.refresh_etf_composition import _justetf_breakdown, _justetf_load_more_path

        html = """
        Wicket.Ajax.ajax({"u":"/en/etf-profile.html?0-1.0-holdingsSection-countries-loadMoreCountries&amp;isin=IE00BK5BQT80","c":"id6"});
        <tr data-testid="etf-holdings_countries_row">
          <td data-testid="tl_etf-holdings_countries_value_name">United States</td>
          <td><span data-testid="tl_etf-holdings_countries_value_percentage">59.77%</span></td>
        </tr>
        """
        self.assertIn("loadMoreCountries", _justetf_load_more_path(html, "countries"))
        expanded = """
        <tr data-testid="etf-holdings_countries_row">
          <td data-testid="tl_etf-holdings_countries_value_name">United States</td>
          <td><span data-testid="tl_etf-holdings_countries_value_percentage">59.77%</span></td>
        </tr>
        <tr data-testid="etf-holdings_countries_row">
          <td data-testid="tl_etf-holdings_countries_value_name">Canada</td>
          <td><span data-testid="tl_etf-holdings_countries_value_percentage">2.95%</span></td>
        </tr>
        """

        class _Opener:
            def open(self, request, timeout=0):
                del request, timeout
                return _Body(expanded)

        class _Body:
            def __init__(self, text: str) -> None:
                self.text = text

            def read(self) -> bytes:
                return self.text.encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        rows = _justetf_breakdown(html, _Opener(), "https://www.justetf.com/en/etf-profile.html?isin=IE00BK5BQT80", "countries")
        self.assertEqual(set(rows), {"United States", "Canada"})


if __name__ == "__main__":
    unittest.main()
