from datetime import date
from decimal import Decimal
from sqlalchemy import extract, func
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from ..extensions import db
from ..models import Bill, Expense, Category
from ..services.cache import cache_get, cache_set, dashboard_summary_key

bp = Blueprint("dashboard", __name__)


@bp.get("/summary")
@jwt_required()
def dashboard_summary():
    uid = int(get_jwt_identity())
    ym = (request.args.get("month") or date.today().strftime("%Y-%m")).strip()
    if not _is_valid_month(ym):
        return jsonify(error="invalid month, expected YYYY-MM"), 400

    key = dashboard_summary_key(uid, ym)
    cached = cache_get(key)
    if cached:
        return jsonify(cached)

    payload = _build_account_summary(uid, ym, account_key=None)
    cache_set(key, payload, ttl_seconds=300)
    return jsonify(payload)


@bp.get("/multi-account-overview")
@jwt_required()
def multi_account_overview():
    uid = int(get_jwt_identity())
    ym = (request.args.get("month") or date.today().strftime("%Y-%m")).strip()
    if not _is_valid_month(ym):
        return jsonify(error="invalid month, expected YYYY-MM"), 400

    account_keys_raw = (request.args.get("account_keys") or "").strip()
    if account_keys_raw:
        try:
            account_keys = [x.strip().upper() for x in account_keys_raw.split(",") if x.strip()]
        except ValueError:
            return jsonify(error="invalid account_keys"), 400
    else:
        account_keys = [
            (x or "UNKNOWN").upper()
            for (x,) in db.session.query(Expense.currency)
            .filter(Expense.user_id == uid)
            .distinct()
            .all()
        ]

    if not account_keys:
        return jsonify(
            period={"month": ym},
            aggregated={
                "monthly_income": 0.0,
                "monthly_expenses": 0.0,
                "net_flow": 0.0,
                "upcoming_bills_total": 0.0,
                "upcoming_bills_count": 0,
            },
            accounts=[],
            errors=[],
        )

    per_accounts = []
    agg_income = Decimal("0")
    agg_expenses = Decimal("0")
    agg_bills_total = Decimal("0")
    agg_bills_count = 0
    errors = []

    for acc in account_keys:
        p = _build_account_summary(uid, ym, account_key=acc)
        per_accounts.append({"account_key": acc, **p})
        agg_income += Decimal(str(p["summary"].get("monthly_income", 0)))
        agg_expenses += Decimal(str(p["summary"].get("monthly_expenses", 0)))
        agg_bills_total += Decimal(str(p["summary"].get("upcoming_bills_total", 0)))
        agg_bills_count += int(p["summary"].get("upcoming_bills_count", 0))
        errors.extend(p.get("errors", []))

    payload = {
        "period": {"month": ym},
        "aggregated": {
            "monthly_income": float(round(agg_income, 2)),
            "monthly_expenses": float(round(agg_expenses, 2)),
            "net_flow": float(round(agg_income - agg_expenses, 2)),
            "upcoming_bills_total": float(round(agg_bills_total, 2)),
            "upcoming_bills_count": agg_bills_count,
        },
        "accounts": per_accounts,
        "errors": sorted(list(set(errors))),
    }
    return jsonify(payload)


def _build_account_summary(uid: int, ym: str, account_key: str | None):
    payload = {
        "period": {"month": ym, "account_key": account_key},
        "summary": {
            "net_flow": 0.0,
            "monthly_income": 0.0,
            "monthly_expenses": 0.0,
            "upcoming_bills_total": 0.0,
            "upcoming_bills_count": 0,
        },
        "recent_transactions": [],
        "upcoming_bills": [],
        "category_breakdown": [],
        "errors": [],
    }

    year, month = map(int, ym.split("-"))
    today = date.today()

    try:
        iq = db.session.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
            Expense.user_id == uid,
            extract("year", Expense.spent_at) == year,
            extract("month", Expense.spent_at) == month,
            Expense.expense_type == "INCOME",
        )
        eq = db.session.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
            Expense.user_id == uid,
            extract("year", Expense.spent_at) == year,
            extract("month", Expense.spent_at) == month,
            Expense.expense_type != "INCOME",
        )
        if account_key is not None:
            iq = iq.filter(Expense.currency == account_key)
            eq = eq.filter(Expense.currency == account_key)

        income = iq.scalar()
        expenses = eq.scalar()

        payload["summary"]["monthly_income"] = float(income or 0)
        payload["summary"]["monthly_expenses"] = float(expenses or 0)
        payload["summary"]["net_flow"] = round(
            payload["summary"]["monthly_income"] - payload["summary"]["monthly_expenses"], 2
        )
    except Exception:
        payload["errors"].append("summary_unavailable")

    try:
        tq = db.session.query(Expense).filter(Expense.user_id == uid)
        if account_key is not None:
            tq = tq.filter(Expense.currency == account_key)
        rows = tq.order_by(Expense.spent_at.desc(), Expense.id.desc()).limit(10).all()
        payload["recent_transactions"] = [
            {
                "id": e.id,
                "description": e.notes or "Transaction",
                "amount": float(e.amount),
                "date": e.spent_at.isoformat(),
                "type": e.expense_type,
                "category_id": e.category_id,
                "account_key": e.currency,
                "currency": e.currency,
            }
            for e in rows
        ]
    except Exception:
        payload["errors"].append("recent_transactions_unavailable")

    try:
        bills = (
            db.session.query(Bill)
            .filter(Bill.user_id == uid, Bill.active.is_(True), Bill.next_due_date >= today)
            .order_by(Bill.next_due_date.asc())
            .limit(8)
            .all()
        )
        payload["upcoming_bills"] = [
            {
                "id": b.id,
                "name": b.name,
                "amount": float(b.amount),
                "currency": b.currency,
                "next_due_date": b.next_due_date.isoformat(),
                "cadence": b.cadence.value,
                "channel_email": b.channel_email,
                "channel_whatsapp": b.channel_whatsapp,
            }
            for b in bills
        ]
        payload["summary"]["upcoming_bills_total"] = round(sum(float(b.amount) for b in bills), 2)
        payload["summary"]["upcoming_bills_count"] = len(bills)
    except Exception:
        payload["errors"].append("upcoming_bills_unavailable")

    try:
        cq = (
            db.session.query(
                Expense.category_id,
                func.coalesce(Category.name, "Uncategorized").label("category_name"),
                func.coalesce(func.sum(Expense.amount), 0).label("total_amount"),
            )
            .outerjoin(Category, (Category.id == Expense.category_id) & (Category.user_id == uid))
            .filter(
                Expense.user_id == uid,
                extract("year", Expense.spent_at) == year,
                extract("month", Expense.spent_at) == month,
                Expense.expense_type != "INCOME",
            )
        )
        if account_key is not None:
            cq = cq.filter(Expense.currency == account_key)

        category_rows = (
            cq.group_by(Expense.category_id, Category.name)
            .order_by(func.sum(Expense.amount).desc())
            .all()
        )

        total = sum(float(r.total_amount or 0) for r in category_rows)
        payload["category_breakdown"] = [
            {
                "category_id": r.category_id,
                "category_name": r.category_name,
                "amount": float(r.total_amount or 0),
                "share_pct": (round((float(r.total_amount or 0) / total) * 100, 2) if total > 0 else 0),
            }
            for r in category_rows
        ]
    except Exception:
        payload["errors"].append("category_breakdown_unavailable")

    return payload


def _is_valid_month(ym: str) -> bool:
    if len(ym) != 7 or ym[4] != "-":
        return False
    year, month = ym.split("-")
    if not (year.isdigit() and month.isdigit()):
        return False
    m = int(month)
    return 1 <= m <= 12
