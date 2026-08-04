"""Double-entry ledger and checkpoint state for the Valura Ledger Arena."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

D = Decimal
ZERO = D("0.00")
QZERO = D("0")


def money(value) -> Decimal:
    return D(str(value)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


def quantity(value) -> Decimal:
    return D(str(value))


def fmt_qty(value: Decimal) -> str:
    if not value:
        return "0"
    return format(value.normalize(), "f")


def leg(account: str, customer_id: str, debit=ZERO, credit=ZERO) -> dict:
    return {"account": account, "customer_id": customer_id,
            "debit": str(money(debit)), "credit": str(money(credit))}


TARIFF = {
    "BRK-A": {"classes": {"equity", "etf"}, "brokerage": D("0.0020"),
              "custody": D("0.0004"), "broker_cost": D("0.0009"),
              "custody_cost": D("0.0002"), "minimum": D("1.00"),
              "ticket": D("0.35"), "payable": "2411"},
    "BRK-B": {"classes": {"equity", "bond"}, "brokerage": D("0.0015"),
              "custody": D("0.0005"), "broker_cost": D("0.0008"),
              "custody_cost": D("0.0003"), "minimum": D("2.50"),
              "ticket": D("3.00"), "payable": "2412"},
    "BRK-C": {"classes": {"etf", "bond"}, "brokerage": D("0.0025"),
              "custody": D("0.0003"), "broker_cost": D("0.0012"),
              "custody_cost": D("0.0001"), "minimum": D("0.50"),
              "ticket": D("0.20"), "payable": "2413"},
}


class Rejected(Exception):
    pass


class Book:
    def __init__(self, record_events: bool = True) -> None:
        self.balances = defaultdict(lambda: ZERO)
        self.seen: set[str] = set()
        self.todo = defaultdict(int)
        self.events: dict[str, dict] = {}
        self.event_legs: dict[str, list[dict]] = {}
        self.event_meta: dict[str, dict] = {}
        self.fees: dict[str, tuple[str, Decimal]] = {}
        self.refunded_fees: set[str] = set()
        self.withdrawals: dict[str, tuple[str, Decimal, str]] = {}
        self.orders: dict[str, dict] = {}
        self.orphan_fills = defaultdict(lambda: {"quantity": QZERO, "final": False})
        self.trades: dict[str, dict] = {}
        self.lots = defaultdict(list)  # (customer, symbol) -> [{quantity,cost,event_id}]
        self.record_events = record_events
        self.event_sequence: list[dict] = []

    def apply(self, ev: dict) -> list[dict]:
        eid = ev.get("event_id")
        if not eid or eid in self.seen:
            return []
        self.seen.add(eid)
        handler = getattr(self, "on_" + str(ev.get("type")), None)
        if handler is None:
            self.todo[str(ev.get("type"))] += 1
            if self.record_events:
                self.event_sequence.append(deepcopy(ev))
            return []
        try:
            legs = handler(ev.get("payload", {}), ev) or []
            self._post(legs)
        except (Rejected, InvalidOperation, KeyError, ValueError, TypeError):
            legs = []
        self.events[eid] = deepcopy(ev)
        self.event_legs[eid] = deepcopy(legs)
        if self.record_events:
            self.event_sequence.append(deepcopy(ev))
        return legs

    def _post(self, legs: list[dict]) -> None:
        dr = sum((D(x["debit"]) for x in legs), ZERO)
        cr = sum((D(x["credit"]) for x in legs), ZERO)
        if money(dr) != money(cr):
            raise AssertionError(f"unbalanced: dr {dr} cr {cr}")
        for x in legs:
            self.balances[(x["customer_id"], x["account"])] += D(x["debit"]) - D(x["credit"])

    def on_deposit(self, p, ev):
        a, c = money(p["amount"]), p["customer_id"]
        return [leg("1100", c, debit=a), leg("2010", c, credit=a)]

    def on_fee_charged(self, p, ev):
        a, c = money(p["amount"]), p["customer_id"]
        self.fees[ev["event_id"]] = (c, a)
        return [leg("2010", c, debit=a), leg("1100", c, credit=a)]

    def on_fee_refund(self, p, ev):
        source = p["refunds_source_id"]
        if source not in self.fees or source in self.refunded_fees:
            raise Rejected
        c, a = self.fees[source]
        if p["customer_id"] != c:
            raise Rejected
        self.refunded_fees.add(source)
        return [leg("1100", c, debit=a), leg("2010", c, credit=a)]

    def on_interest_credited(self, p, ev):
        c, gross, share = p["customer_id"], money(p["gross_amount"]), money(p["customer_share"])
        if share > gross:
            raise Rejected
        return [leg("1100", c, debit=gross), leg("2010", c, credit=share),
                leg("4200", c, credit=money(gross - share))]

    def on_transfer_between_customers(self, p, ev):
        a = money(p["amount"])
        return [leg("2010", p["from_customer_id"], debit=a),
                leg("2010", p["to_customer_id"], credit=a)]

    def on_fx_deposit(self, p, ev):
        market, customer = money(p["usd_at_market_rate"]), money(p["usd_at_customer_rate"])
        if customer > market:
            raise Rejected
        c = p["customer_id"]
        return [leg("1100", c, debit=market), leg("2010", c, credit=customer),
                leg("4100", c, credit=money(market - customer))]

    def on_withdrawal_requested(self, p, ev):
        a, c, wid = money(p["amount"]), p["customer_id"], p["withdrawal_id"]
        if wid in self.withdrawals:
            raise Rejected
        self.withdrawals[wid] = (c, a, "open")
        return [leg("2010", c, debit=a), leg("2300", c, credit=a)]

    def _withdrawal_close(self, p, settled):
        wid = p["withdrawal_id"]
        if wid not in self.withdrawals or self.withdrawals[wid][2] != "open":
            raise Rejected
        c, a, _ = self.withdrawals[wid]
        self.withdrawals[wid] = (c, a, "settled" if settled else "rejected")
        return ([leg("2300", c, debit=a), leg("1100", c, credit=a)] if settled else
                [leg("2300", c, debit=a), leg("2010", c, credit=a)])

    def on_withdrawal_settled(self, p, ev): return self._withdrawal_close(p, True)
    def on_withdrawal_rejected(self, p, ev): return self._withdrawal_close(p, False)

    def _route(self, asset_class, principal):
        candidates = []
        for broker, t in TARIFF.items():
            if asset_class in t["classes"]:
                brokerage = max(money(principal * t["brokerage"]), t["minimum"])
                custody = money(principal * t["custody"])
                candidates.append((brokerage + custody, broker))
        if not candidates:
            raise Rejected
        return min(candidates)[1]

    def on_order_placed(self, p, ev):
        oid, c = p["order_id"], p["customer_id"]
        if oid in self.orders:
            raise Rejected
        q, px = quantity(p["quantity"]), D(str(p["limit_price"]))
        hold = money(q * px + D(str(p["est_charges"]))) if p["side"] == "buy" else ZERO
        prior = self.orphan_fills.get(oid, {"quantity": QZERO, "final": False})
        if prior["quantity"] > q:
            raise Rejected
        self.orphan_fills.pop(oid, None)
        remaining = q - prior["quantity"]
        remaining_hold = money(hold * remaining / q) if q and p["side"] == "buy" else ZERO
        is_open = not prior["final"] and remaining > 0
        self.orders[oid] = {"customer_id": c, "side": p["side"], "symbol": p["symbol"],
                            "quantity": q, "remaining": remaining, "hold": hold,
                            "remaining_hold": remaining_hold if is_open else ZERO,
                            "asset_class": p["asset_class"],
                            "route": self._route(p["asset_class"], q * px), "open": is_open}
        return []

    def _charges(self, broker, principal, partner_rate):
        t = TARIFF[broker]
        brokerage = max(money(principal * t["brokerage"]), t["minimum"])
        custody = money(principal * t["custody"])
        regulatory = money(principal * D("0.0008"))
        broker_cost = money(principal * t["broker_cost"] + t["ticket"])
        custody_cost = money(principal * t["custody_cost"])
        margin = brokerage + custody - broker_cost - custody_cost
        partner = money(max(ZERO, margin) * D(str(partner_rate)))
        return brokerage, custody, regulatory, broker_cost, custody_cost, partner, t["payable"]

    def _consume_fifo(self, c, symbol, qty):
        available = sum((x["quantity"] for x in self.lots[(c, symbol)]), QZERO)
        if qty <= 0 or available < qty:
            raise Rejected
        remaining, cost, consumed = qty, ZERO, []
        for lot in self.lots[(c, symbol)]:
            if not remaining:
                break
            take = min(remaining, lot["quantity"])
            relieved = lot["cost"] if take == lot["quantity"] else money(lot["cost"] * take / lot["quantity"])
            consumed.append({"event_id": lot["event_id"], "quantity": take, "cost": relieved})
            lot["quantity"] -= take
            lot["cost"] -= relieved
            cost += relieved
            remaining -= take
        self.lots[(c, symbol)] = [x for x in self.lots[(c, symbol)] if x["quantity"]]
        return money(cost), consumed

    def on_order_partially_filled(self, p, ev): return self._fill(p, ev, False)
    def on_order_filled(self, p, ev): return self._fill(p, ev, True)

    def _fill(self, p, ev, final):
        c, side, symbol = p["customer_id"], p["side"], p["symbol"]
        qty, principal = quantity(p["quantity"]), money(p["principal"])
        broker = p["broker"]
        if qty <= 0:
            raise Rejected
        if broker not in TARIFF or p["asset_class"] not in TARIFF[broker]["classes"]:
            raise Rejected
        b, cu, reg, bc, cc, ps, payable = self._charges(broker, principal, p["partner_rate"])
        oid = p["order_id"]
        order = self.orders.get(oid)
        if order and (order["customer_id"] != c or order["side"] != side or order["symbol"] != symbol
                      or qty > order["remaining"]):
            raise Rejected
        consumed = []
        if side == "buy":
            cost = principal
            self.lots[(c, symbol)].append({"quantity": qty, "cost": cost, "event_id": ev["event_id"]})
            customer_amount = money(principal + b + cu + reg)
            legs = [leg("2010", c, debit=customer_amount), leg("1200", c, debit=principal),
                    leg("5000", c, debit=bc), leg("5010", c, debit=cc), leg("5100", c, debit=ps),
                    leg("2350", c, credit=principal), leg("2100", c, credit=principal),
                    leg("4000", c, credit=b), leg("4010", c, credit=cu), leg("2400", c, credit=reg),
                    leg(payable, c, credit=bc), leg("2420", c, credit=cc), leg("2430", c, credit=ps)]
        elif side == "sell":
            cost, consumed = self._consume_fifo(c, symbol, qty)
            proceeds = money(principal - b - cu - reg)
            legs = [leg("1150", c, debit=principal), leg("2100", c, debit=cost),
                    leg("5000", c, debit=bc), leg("5010", c, debit=cc), leg("5100", c, debit=ps),
                    leg("2010", c, credit=proceeds), leg("1200", c, credit=cost),
                    leg("4000", c, credit=b), leg("4010", c, credit=cu), leg("2400", c, credit=reg),
                    leg(payable, c, credit=bc), leg("2420", c, credit=cc), leg("2430", c, credit=ps)]
        else:
            raise Rejected
        if order:
            old_remaining = order["remaining"]
            order["remaining"] -= qty
            if order["side"] == "buy" and old_remaining:
                release = order["remaining_hold"] if final else money(order["remaining_hold"] * qty / old_remaining)
                order["remaining_hold"] -= release
            if final:
                order["remaining_hold"] = ZERO
                order["open"] = False
        else:
            self.orphan_fills[oid]["quantity"] += qty
            self.orphan_fills[oid]["final"] = self.orphan_fills[oid]["final"] or final
        self.trades[p["trade_id"]] = {"customer_id": c, "side": side, "principal": principal,
                                             "event_id": ev["event_id"], "symbol": symbol,
                                             "quantity": qty, "consumed": consumed, "settled": False}
        return legs

    def on_trade_settled(self, p, ev):
        t = self.trades.get(p["trade_id"])
        if not t or t["settled"]:
            raise Rejected
        t["settled"] = True
        c, a = t["customer_id"], t["principal"]
        return ([leg("2350", c, debit=a), leg("1100", c, credit=a)] if t["side"] == "buy" else
                [leg("1100", c, debit=a), leg("1150", c, credit=a)])

    def on_order_cancelled(self, p, ev):
        order = self.orders.get(p["order_id"])
        if not order or not order["open"]:
            raise Rejected
        order["remaining_hold"] = ZERO
        order["open"] = False
        return []

    def on_order_rejected(self, p, ev): return self.on_order_cancelled(p, ev)

    def _settle_payable(self, c, account):
        due = -self.balances[(c, account)]
        if due <= 0:
            raise Rejected
        return [leg(account, c, debit=due), leg("1100", c, credit=due)]

    def on_broker_fees_settled(self, p, ev):
        broker = p["broker"]
        return self._settle_payable(p["customer_id"], TARIFF[broker]["payable"])
    def on_custodian_fees_settled(self, p, ev): return self._settle_payable(p["customer_id"], "2420")
    def on_reg_fees_remitted(self, p, ev): return self._settle_payable(p["customer_id"], "2400")
    def on_partner_payout(self, p, ev): return self._settle_payable(p["customer_id"], "2430")

    def on_dividend_cash(self, p, ev):
        c, net = p["customer_id"], money(p["net_amount"])
        return [leg("1100", c, debit=net), leg("2010", c, credit=net)]

    def on_dividend_reinvested(self, p, ev):
        c, net, q = p["customer_id"], money(p["net_amount"]), quantity(p["reinvest_quantity"])
        if q <= 0:
            raise Rejected
        self.lots[(c, p["symbol"])].append({"quantity": q, "cost": net, "event_id": ev["event_id"]})
        return [leg("1200", c, debit=net), leg("2100", c, credit=net)]

    def on_stock_split(self, p, ev):
        factor = quantity(p["ratio_to"]) / quantity(p["ratio_from"])
        affected = []
        for lot in self.lots[(p["customer_id"], p["symbol"])]:
            affected.append(lot["event_id"])
            lot["quantity"] *= factor
        self.event_meta[ev["event_id"]] = {"kind": "split", "customer_id": p["customer_id"],
                                            "symbol": p["symbol"], "factor": factor,
                                            "affected": affected}
        return []

    def on_symbol_change(self, p, ev):
        c, old, new = p["customer_id"], p["old_symbol"], p["new_symbol"]
        if old == new or not self.lots[(c, old)]:
            raise Rejected
        moved = self.lots.pop((c, old))
        self.event_meta[ev["event_id"]] = {"kind": "symbol", "customer_id": c,
                                            "old": old, "new": new,
                                            "affected": [x["event_id"] for x in moved]}
        self.lots[(c, new)].extend(moved)
        return []

    def on_reversal(self, p, ev):
        source = p["reverses_event_id"]
        if source not in self.event_legs:
            raise Rejected
        original = self.events[source]
        if original.get("_reversed"):
            raise Rejected
        original["_reversed"] = True
        meta = self.event_meta.get(source, {})
        if meta.get("kind") == "split":
            key = (meta["customer_id"], meta["symbol"])
            for lot in self.lots[key]:
                if lot["event_id"] in meta["affected"]:
                    lot["quantity"] /= meta["factor"]
        elif meta.get("kind") == "symbol":
            new_key, old_key = (meta["customer_id"], meta["new"]), (meta["customer_id"], meta["old"])
            staying, returning = [], []
            for lot in self.lots[new_key]:
                (returning if lot["event_id"] in meta["affected"] else staying).append(lot)
            self.lots[new_key] = staying
            self.lots[old_key].extend(returning)
        for lot_key in list(self.lots):
            if meta.get("kind") not in {"split", "symbol"}:
                self.lots[lot_key] = [x for x in self.lots[lot_key] if x["event_id"] != source]
        t = next((x for x in self.trades.values() if x["event_id"] == source), None)
        if t and t["side"] == "sell":
            key = (t["customer_id"], t["symbol"])
            for used in reversed(t["consumed"]):
                self.lots[key].insert(0, {"quantity": used["quantity"], "cost": used["cost"],
                                          "event_id": used["event_id"]})
        return [leg(x["account"], x["customer_id"], debit=x["credit"], credit=x["debit"])
                for x in self.event_legs[source]]

    def _state_snapshot(self):
        tb = defaultdict(lambda: ZERO)
        for (_c, a), b in self.balances.items(): tb[a] += b
        customers = {}
        cids = {c for c, _a in self.balances} | {c for c, _s in self.lots} | {o["customer_id"] for o in self.orders.values()}
        for c in cids:
            positions = {}
            for (cid, symbol), lots in self.lots.items():
                if cid != c: continue
                q = sum((x["quantity"] for x in lots), QZERO)
                cost = sum((x["cost"] for x in lots), ZERO)
                if q:
                    positions[symbol] = {"quantity": fmt_qty(q), "cost_basis": str(money(cost))}
            hold = sum((o["remaining_hold"] for o in self.orders.values()
                        if o["customer_id"] == c and o["open"] and o["side"] == "buy"), ZERO)
            customers[c] = {"wallet_cash": str(money(-self.balances[(c, "2010")])),
                            "cash_hold": str(money(hold)), "positions": positions}
        routes = {oid: o["route"] for oid, o in self.orders.items() if o["open"]}
        return {"trial_balance": {a: str(money(v)) for a, v in sorted(tb.items())},
                "customers": {c: customers[c] for c in sorted(customers)},
                "open_order_routes": dict(sorted(routes.items()))}

    def snapshot(self, as_of_event_id=None) -> dict:
        if as_of_event_id is not None:
            replay = Book(record_events=False)
            for ev in self.event_sequence:
                replay.apply(ev)
                if ev["event_id"] == as_of_event_id:
                    return replay._state_snapshot()
            raise Rejected
        return self._state_snapshot()
