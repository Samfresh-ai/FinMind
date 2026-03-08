from datetime import date, timedelta
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func
from ..extensions import db
from ..models import Expense, Category
from ..services.ai import monthly_budget_suggestion
import logging

bp = Blueprint("insights", __name__)
logger = logging.getLogger("finmind.insights")


@bp.get("/budget-suggestion")
@jwt_required()
def budget_suggestion():
    uid = int(get_jwt_identity())
    ym = (request.args.get("month") or date.today().strftime("%Y-%m")).strip()
    user_gemini_key = (request.headers.get("X-Gemini-Api-Key") or "").strip() or None
    persona = (request.headers.get("X-Insight-Persona") or "").strip() or None
    suggestion = monthly_budget_suggestion(
        uid,
        ym,
        gemini_api_key=user_gemini_key,
        persona=persona,
    )
    logger.info("Budget suggestion served user=%s month=%s", uid, ym)
    return jsonify(suggestion)


@bp.get("/weekly-digest")
@jwt_required()
def weekly_digest():
    uid = int(get_jwt_identity())

    end_raw = (request.args.get("end_date") or "").strip()
    try:
        end_date = date.fromisoformat(end_raw) if end_raw else date.today()
    except ValueError:
        return jsonify(error="invalid end_date"), 400

    # Current 7-day window (inclusive): [end_date-6, end_date]
    current_start = end_date - timedelta(days=6)
    current_end = end_date

    # Previous 7-day window (inclusive): [current_start-7, current_start-1]
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=6)

    def _category_totals(start: date, end: date):
        rows = (
            db.session.query(
                Expense.category_id,
                Category.name,
                func.sum(Expense.amount).label("total"),
            )
            .outerjoin(Category, Category.id == Expense.category_id)
            .filter(Expense.user_id == uid)
            .filter(Expense.spent_at >= start)
            .filter(Expense.spent_at <= end)
            .group_by(Expense.category_id, Category.name)
            .all()
        )
        out = {}
        for category_id, name, total in rows:
            key = (name or "Uncategorized").strip() or "Uncategorized"
            out[key] = float(total or 0)
        return out

    current_totals = _category_totals(current_start, current_end)
    previous_totals = _category_totals(previous_start, previous_end)

    current_total = float(sum(current_totals.values()))
    previous_total = float(sum(previous_totals.values()))

    if previous_total > 0:
        wow_change_pct = ((current_total - previous_total) / previous_total) * 100.0
    else:
        wow_change_pct = 0.0 if current_total == 0 else 100.0

    categories = []
    all_keys = set(current_totals.keys()) | set(previous_totals.keys())
    for key in sorted(all_keys):
        cur = float(current_totals.get(key, 0.0))
        prev = float(previous_totals.get(key, 0.0))
        if prev > 0:
            delta_pct = ((cur - prev) / prev) * 100.0
        else:
            delta_pct = 0.0 if cur == 0 else 100.0
        categories.append(
            {
                "category": key,
                "current": round(cur, 2),
                "previous": round(prev, 2),
                "delta": round(cur - prev, 2),
                "delta_pct": round(delta_pct, 2),
            }
        )

    # Insight bullets (simple deterministic heuristics)
    insights = []
    if current_total == 0:
        insights.append("No spending activity recorded in the last 7 days.")
    else:
        top = max(categories, key=lambda x: x["current"], default=None)
        if top and top["current"] > 0:
            insights.append(
                f"Top spending category this week: {top['category']} ({top['current']:.2f})."
            )
        if wow_change_pct >= 15:
            insights.append("Spending increased significantly vs previous week.")
        elif wow_change_pct <= -15:
            insights.append("Spending decreased significantly vs previous week.")
        else:
            insights.append("Spending remained relatively stable week-over-week.")

    payload = {
        "period": {
            "current": {
                "start": current_start.isoformat(),
                "end": current_end.isoformat(),
            },
            "previous": {
                "start": previous_start.isoformat(),
                "end": previous_end.isoformat(),
            },
        },
        "summary": {
            "current_total": round(current_total, 2),
            "previous_total": round(previous_total, 2),
            "week_over_week_change_pct": round(wow_change_pct, 2),
            "currency": "USD",
        },
        "categories": categories,
        "insights": insights,
    }

    logger.info("Weekly digest served user=%s end_date=%s", uid, end_date.isoformat())
    return jsonify(payload)
