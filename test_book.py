from decimal import Decimal

from book import Book, money


def event(n, kind, **payload):
    return {"event_id": f"e{n}", "type": kind, "payload": payload}


def balanced(legs):
    return sum(Decimal(x["debit"]) for x in legs) == sum(Decimal(x["credit"]) for x in legs)


def test_cash_idempotency_and_historical_snapshot():
    b = Book()
    e1 = event(1, "deposit", customer_id="C1", amount="100.00")
    assert balanced(b.apply(e1))
    assert b.apply(e1) == []
    assert b.last_duplicate is True
    b.apply(event(2, "fee_charged", customer_id="C1", amount="3.25"))
    assert b.snapshot("e1")["customers"]["C1"]["wallet_cash"] == "100.00"
    assert b.snapshot()["customers"]["C1"]["wallet_cash"] == "96.75"


def test_withdrawal_and_refund_references():
    b = Book()
    b.apply(event(1, "fee_charged", customer_id="C1", amount="2"))
    assert balanced(b.apply(event(2, "fee_refund", customer_id="C1", refunds_source_id="e1")))
    assert b.apply(event(3, "fee_refund", customer_id="C1", refunds_source_id="e1")) == []
    b.apply(event(4, "withdrawal_requested", customer_id="C1", withdrawal_id="w1", amount="10"))
    assert balanced(b.apply(event(5, "withdrawal_settled", withdrawal_id="w1")))


def test_buy_sell_fifo_fees_settlement_and_reversal():
    b = Book()
    b.apply(event(0, "deposit", customer_id="C1", amount="10000"))
    b.apply(event(1, "order_placed", order_id="o1", customer_id="C1", side="buy",
                  symbol="XYZ", quantity="10", limit_price="100", asset_class="equity",
                  est_charges="5"))
    buy = b.apply(event(2, "order_filled", order_id="o1", customer_id="C1", side="buy",
                        symbol="XYZ", quantity="10", price="99", principal="990",
                        asset_class="equity", broker="BRK-A", partner_rate="0.5", trade_id="t1"))
    assert balanced(buy)
    assert b.snapshot()["customers"]["C1"]["positions"]["XYZ"] == {
        "quantity": "10", "cost_basis": "990.00"}
    assert balanced(b.apply(event(3, "trade_settled", trade_id="t1")))
    b.apply(event(4, "order_placed", order_id="o2", customer_id="C1", side="sell",
                  symbol="XYZ", quantity="4", limit_price="120", asset_class="equity",
                  est_charges="4"))
    sell = b.apply(event(5, "order_filled", order_id="o2", customer_id="C1", side="sell",
                         symbol="XYZ", quantity="4", price="120", principal="480",
                         asset_class="equity", broker="BRK-A", partner_rate="0.25", trade_id="t2"))
    assert balanced(sell)
    assert b.snapshot()["customers"]["C1"]["positions"]["XYZ"] == {
        "quantity": "6", "cost_basis": "594.00"}
    assert balanced(b.apply(event(6, "broker_fees_settled", customer_id="C1", broker="BRK-A")))
    assert balanced(b.apply(event(7, "custodian_fees_settled", customer_id="C1")))
    assert balanced(b.apply(event(8, "reg_fees_remitted", customer_id="C1")))
    assert balanced(b.apply(event(9, "partner_payout", customer_id="C1")))


def test_corporate_actions_fx_and_routing():
    b = Book()
    b.apply(event(1, "dividend_reinvested", customer_id="C1", symbol="ETF",
                  gross_amount="11", withholding_tax="1", net_amount="10",
                  reinvest_price="5", reinvest_quantity="2"))
    b.apply(event(2, "stock_split", customer_id="C1", symbol="ETF", ratio_from="1", ratio_to="3"))
    b.apply(event(3, "symbol_change", customer_id="C1", old_symbol="ETF", new_symbol="ETF2"))
    assert b.snapshot()["customers"]["C1"]["positions"]["ETF2"] == {
        "quantity": "6", "cost_basis": "10.00"}
    assert balanced(b.apply(event(4, "fx_deposit", customer_id="C1", amount_foreign="100",
                                  currency="EUR", market_rate="1.1", customer_rate="1.08",
                                  usd_at_market_rate="110", usd_at_customer_rate="108")))
    assert b.apply(event(5, "fx_deposit", customer_id="C1", amount_foreign="100",
                         currency="EUR", market_rate="1.1", customer_rate="1.2",
                         usd_at_market_rate="110", usd_at_customer_rate="120")) == []
    b.apply(event(6, "order_placed", order_id="o", customer_id="C1", side="buy",
                  symbol="BOND", quantity="1", limit_price="1000", asset_class="bond",
                  est_charges="5"))
    assert b.snapshot()["open_order_routes"]["o"] == "BRK-C"


def test_half_away_rounding():
    assert money("1.005") == Decimal("1.01")
    assert money("-1.005") == Decimal("-1.01")
