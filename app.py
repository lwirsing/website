from __future__ import annotations

import calendar
import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="Monarch Budget Planner", layout="wide")

DB_PATH = Path("finance_data.db")


@dataclass
class Bill:
    bill_id: int
    name: str
    amount: float
    recurrence: str
    due_date: str | None
    due_day: int | None
    category: str | None
    active: bool
    notes: str | None


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                row_hash TEXT UNIQUE,
                tx_date TEXT NOT NULL,
                merchant TEXT,
                category TEXT,
                account TEXT,
                original_statement TEXT,
                notes TEXT,
                amount REAL NOT NULL,
                tags TEXT,
                owner TEXT,
                business_entity TEXT,
                source_file TEXT,
                imported_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                month TEXT NOT NULL,
                category TEXT NOT NULL,
                amount REAL NOT NULL,
                notes TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (month, category)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                amount REAL NOT NULL,
                recurrence TEXT NOT NULL,
                due_date TEXT,
                due_day INTEGER,
                category TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


@st.cache_data(show_spinner=False)
def sample_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path)


def clean_monarch_csv(df: pd.DataFrame) -> pd.DataFrame:
    required = {
        "Date",
        "Merchant",
        "Category",
        "Account",
        "Original Statement",
        "Notes",
        "Amount",
        "Tags",
        "Owner",
        "Business Entity",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")

    out = df.copy()
    out["Date"] = pd.to_datetime(out["Date"], errors="coerce")
    out["Amount"] = pd.to_numeric(out["Amount"], errors="coerce")
    out = out.dropna(subset=["Date", "Amount"])
    out["Month"] = out["Date"].dt.to_period("M").astype(str)
    out = out.sort_values("Date").reset_index(drop=True)
    return out


def row_hash(row: pd.Series) -> str:
    payload = "|".join(
        [
            str(row.get("Date", "")),
            str(row.get("Merchant", "")),
            str(row.get("Category", "")),
            str(row.get("Account", "")),
            str(row.get("Original Statement", "")),
            str(row.get("Notes", "")),
            f"{float(row.get('Amount', 0.0)):.2f}",
            str(row.get("Tags", "")),
            str(row.get("Owner", "")),
            str(row.get("Business Entity", "")),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def import_transactions(df: pd.DataFrame, source_file: str) -> tuple[int, int]:
    cleaned = clean_monarch_csv(df)
    imported_at = datetime.utcnow().isoformat(timespec="seconds")
    inserted = 0
    skipped = 0
    with get_conn() as conn:
        for _, row in cleaned.iterrows():
            rec = {
                "row_hash": row_hash(row),
                "tx_date": row["Date"].date().isoformat(),
                "merchant": row.get("Merchant"),
                "category": row.get("Category"),
                "account": row.get("Account"),
                "original_statement": row.get("Original Statement"),
                "notes": row.get("Notes"),
                "amount": float(row["Amount"]),
                "tags": row.get("Tags"),
                "owner": row.get("Owner"),
                "business_entity": row.get("Business Entity"),
                "source_file": source_file,
                "imported_at": imported_at,
            }
            try:
                conn.execute(
                    """
                    INSERT INTO transactions (
                        row_hash, tx_date, merchant, category, account, original_statement,
                        notes, amount, tags, owner, business_entity, source_file, imported_at
                    ) VALUES (
                        :row_hash, :tx_date, :merchant, :category, :account, :original_statement,
                        :notes, :amount, :tags, :owner, :business_entity, :source_file, :imported_at
                    )
                    """,
                    rec,
                )
                inserted += 1
            except sqlite3.IntegrityError:
                skipped += 1
    return inserted, skipped


@st.cache_data(show_spinner=False)
def load_all_transactions(_refresh: int = 0) -> pd.DataFrame:
    with get_conn() as conn:
        df = pd.read_sql_query("SELECT * FROM transactions", conn)
    if df.empty:
        return df
    df["tx_date"] = pd.to_datetime(df["tx_date"], errors="coerce")
    df["month"] = df["tx_date"].dt.to_period("M").astype(str)
    return df


def load_budgets(month: str | None = None) -> pd.DataFrame:
    query = "SELECT month, category, amount, notes, updated_at FROM budgets"
    params: tuple = ()
    if month:
        query += " WHERE month = ?"
        params = (month,)
    with get_conn() as conn:
        df = pd.read_sql_query(query, conn, params=params)
    return df


def save_budget_rows(month: str, rows: pd.DataFrame) -> int:
    if rows.empty:
        return 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    saved = 0
    with get_conn() as conn:
        for _, row in rows.iterrows():
            category = str(row.get("Category", "")).strip()
            if not category:
                continue
            amount = pd.to_numeric(pd.Series([row.get("Budget")]), errors="coerce").iloc[0]
            if pd.isna(amount):
                continue
            notes = row.get("Notes")
            conn.execute(
                """
                INSERT INTO budgets(month, category, amount, notes, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(month, category) DO UPDATE SET
                    amount=excluded.amount,
                    notes=excluded.notes,
                    updated_at=excluded.updated_at
                """,
                (month, category, float(amount), None if pd.isna(notes) else str(notes), now),
            )
            saved += 1
    return saved


def list_bills(active_only: bool = True) -> list[Bill]:
    query = "SELECT * FROM bills"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name"

    with get_conn() as conn:
        rows = conn.execute(query).fetchall()

    return [
        Bill(
            bill_id=row["id"],
            name=row["name"],
            amount=float(row["amount"]),
            recurrence=row["recurrence"],
            due_date=row["due_date"],
            due_day=row["due_day"],
            category=row["category"],
            active=bool(row["active"]),
            notes=row["notes"],
        )
        for row in rows
    ]


def add_bill(
    name: str,
    amount: float,
    recurrence: str,
    due_date: date | None,
    due_day: int | None,
    category: str | None,
    notes: str | None,
) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO bills(name, amount, recurrence, due_date, due_day, category, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                amount,
                recurrence,
                due_date.isoformat() if due_date else None,
                due_day,
                category.strip() if category else None,
                notes.strip() if notes else None,
                now,
                now,
            ),
        )


def set_bill_active(bill_id: int, active: bool) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds")
    with get_conn() as conn:
        conn.execute(
            "UPDATE bills SET active = ?, updated_at = ? WHERE id = ?",
            (1 if active else 0, now, bill_id),
        )


def upcoming_bill_events(bills: Iterable[Bill], start: date, end: date) -> pd.DataFrame:
    events: list[dict[str, object]] = []
    for bill in bills:
        if not bill.active:
            continue

        if bill.recurrence == "one-time":
            if bill.due_date is None:
                continue
            dt = datetime.strptime(bill.due_date, "%Y-%m-%d").date()
            if start <= dt <= end:
                events.append({"Date": dt, "Bill": bill.name, "Amount": bill.amount, "Category": bill.category})

        elif bill.recurrence == "monthly":
            if bill.due_day is None:
                continue
            y = start.year
            m = start.month
            while date(y, m, 1) <= end:
                day = min(bill.due_day, calendar.monthrange(y, m)[1])
                dt = date(y, m, day)
                if start <= dt <= end:
                    events.append({"Date": dt, "Bill": bill.name, "Amount": bill.amount, "Category": bill.category})
                if m == 12:
                    y += 1
                    m = 1
                else:
                    m += 1

        elif bill.recurrence == "yearly":
            if bill.due_date is None:
                continue
            anchor = datetime.strptime(bill.due_date, "%Y-%m-%d").date()
            for year in range(start.year, end.year + 1):
                day = min(anchor.day, calendar.monthrange(year, anchor.month)[1])
                dt = date(year, anchor.month, day)
                if start <= dt <= end:
                    events.append({"Date": dt, "Bill": bill.name, "Amount": bill.amount, "Category": bill.category})

        elif bill.recurrence == "weekly":
            if bill.due_date is None:
                continue
            current = datetime.strptime(bill.due_date, "%Y-%m-%d").date()
            while current < start:
                current += timedelta(days=7)
            while current <= end:
                events.append({"Date": current, "Bill": bill.name, "Amount": bill.amount, "Category": bill.category})
                current += timedelta(days=7)

    if not events:
        return pd.DataFrame(columns=["Date", "Bill", "Amount", "Category", "Month"])

    df = pd.DataFrame(events)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date")
    df["Month"] = df["Date"].dt.to_period("M").astype(str)
    return df


def monthly_category_actuals(tx_df: pd.DataFrame, month: str, exclude_transfers: bool) -> pd.DataFrame:
    if tx_df.empty:
        return pd.DataFrame(columns=["Category", "Actual"])

    month_df = tx_df[tx_df["month"] == month].copy()
    month_df = month_df[month_df["amount"] < 0]
    if exclude_transfers:
        month_df = month_df[month_df["category"].fillna("").str.lower() != "transfer"]

    if month_df.empty:
        return pd.DataFrame(columns=["Category", "Actual"])

    actuals = (
        month_df.groupby("category", as_index=False)["amount"]
        .sum()
        .rename(columns={"category": "Category", "amount": "Actual"})
    )
    actuals["Actual"] = actuals["Actual"].abs()
    actuals = actuals.sort_values("Actual", ascending=False)
    return actuals


def build_monthly_review(tx_df: pd.DataFrame, month: str, exclude_transfers: bool) -> pd.DataFrame:
    actuals = monthly_category_actuals(tx_df, month, exclude_transfers)
    budgets = load_budgets(month)
    if budgets.empty and actuals.empty:
        return pd.DataFrame(columns=["Category", "Budget", "Actual", "Variance", "Status"])

    merged = actuals.merge(
        budgets[["category", "amount"]].rename(columns={"category": "Category", "amount": "Budget"}),
        on="Category",
        how="outer",
    )
    merged["Budget"] = pd.to_numeric(merged["Budget"], errors="coerce").fillna(0)
    merged["Actual"] = pd.to_numeric(merged["Actual"], errors="coerce").fillna(0)
    merged["Variance"] = merged["Budget"] - merged["Actual"]
    merged["Status"] = merged["Variance"].apply(lambda x: "Within" if x >= 0 else "Over")
    merged = merged.sort_values(["Status", "Actual"], ascending=[True, False])
    return merged


def next_month(month_str: str) -> str:
    dt = datetime.strptime(month_str + "-01", "%Y-%m-%d").date()
    y = dt.year + (1 if dt.month == 12 else 0)
    m = 1 if dt.month == 12 else dt.month + 1
    return f"{y:04d}-{m:02d}"


def fmt_usd(v: float) -> str:
    return f"${v:,.2f}"


def _expense_df(tx_df: pd.DataFrame) -> pd.DataFrame:
    if tx_df.empty:
        return pd.DataFrame()
    df = tx_df[tx_df["amount"] < 0].copy()
    if df.empty:
        return df
    df["spend"] = df["amount"].abs()
    df["merchant_key"] = df["merchant"].fillna("").astype(str).str.strip().str.lower()
    df["category_key"] = df["category"].fillna("").astype(str).str.strip()
    return df


def detect_recurring_purchases(tx_df: pd.DataFrame, min_occurrences: int = 3) -> pd.DataFrame:
    df = _expense_df(tx_df)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "Merchant",
                "Category",
                "Occurrences",
                "Months Active",
                "Avg Amount",
                "Median Days Between",
                "Pattern",
                "Amount Stability",
                "Estimated Monthly Cost",
                "Confidence",
            ]
        )

    candidates: list[dict[str, object]] = []
    grouped = df.groupby(["merchant_key", "category_key"], dropna=False)
    for (_, _), grp in grouped:
        if len(grp) < min_occurrences:
            continue
        g = grp.sort_values("tx_date")
        spend = g["spend"].astype(float)
        dates = pd.to_datetime(g["tx_date"]).dropna().sort_values()
        if len(dates) < min_occurrences:
            continue

        diffs = dates.diff().dt.days.dropna()
        if diffs.empty:
            continue

        median_gap = float(diffs.median())
        monthly_ratio = float(((diffs >= 20) & (diffs <= 40)).mean()) if len(diffs) else 0.0
        weekly_ratio = float(((diffs >= 5) & (diffs <= 10)).mean()) if len(diffs) else 0.0
        amount_cv = float(spend.std(ddof=0) / spend.mean()) if spend.mean() > 0 else 1.0
        stability = max(0.0, min(1.0, 1.0 - amount_cv))

        pattern = "other"
        est_monthly = float(spend.mean())
        pattern_score = 0.0

        if 20 <= median_gap <= 40 and monthly_ratio >= 0.55:
            pattern = "monthly"
            est_monthly = float(spend.mean())
            pattern_score = monthly_ratio
        elif 5 <= median_gap <= 10 and weekly_ratio >= 0.55:
            pattern = "weekly"
            est_monthly = float(spend.mean() * 4.33)
            pattern_score = weekly_ratio

        confidence = max(0.0, min(1.0, 0.6 * pattern_score + 0.4 * stability))
        if pattern == "other" or confidence < 0.45:
            continue

        candidates.append(
            {
                "Merchant": str(g["merchant"].iloc[0]),
                "Category": str(g["category"].iloc[0]),
                "Occurrences": int(len(g)),
                "Months Active": int(g["month"].nunique()),
                "Avg Amount": float(spend.mean()),
                "Median Days Between": round(median_gap, 1),
                "Pattern": pattern,
                "Amount Stability": round(stability, 2),
                "Estimated Monthly Cost": round(est_monthly, 2),
                "Confidence": round(confidence, 2),
            }
        )

    if not candidates:
        return pd.DataFrame(
            columns=[
                "Merchant",
                "Category",
                "Occurrences",
                "Months Active",
                "Avg Amount",
                "Median Days Between",
                "Pattern",
                "Amount Stability",
                "Estimated Monthly Cost",
                "Confidence",
            ]
        )

    out = pd.DataFrame(candidates).sort_values(
        ["Estimated Monthly Cost", "Confidence"], ascending=[False, False]
    )
    return out


def monthly_total_expenses(tx_df: pd.DataFrame, year: int) -> pd.DataFrame:
    df = _expense_df(tx_df)
    if df.empty:
        return pd.DataFrame(columns=["month", "total_spend"])
    year_df = df[df["tx_date"].dt.year == year].copy()
    if year_df.empty:
        return pd.DataFrame(columns=["month", "total_spend"])
    monthly = year_df.groupby("month", as_index=False)["spend"].sum().sort_values("month")
    monthly = monthly.rename(columns={"spend": "total_spend"})
    return monthly


def monthly_category_spend(tx_df: pd.DataFrame, year: int) -> pd.DataFrame:
    df = _expense_df(tx_df)
    if df.empty:
        return pd.DataFrame(columns=["month", "category", "spend"])
    year_df = df[df["tx_date"].dt.year == year].copy()
    if year_df.empty:
        return pd.DataFrame(columns=["month", "category", "spend"])
    return (
        year_df.groupby(["month", "category"], as_index=False)["spend"]
        .sum()
        .sort_values(["month", "spend"], ascending=[True, False])
    )


def get_ai_savings_recommendations(
    tx_df: pd.DataFrame,
    recurring_df: pd.DataFrame,
    monthly_reduction_goal: float,
    user_context: str,
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return "Set OPENAI_API_KEY to enable ChatGPT-powered savings recommendations in this tab."

    try:
        from openai import OpenAI
    except Exception:
        return "Install the OpenAI SDK (`pip install openai`) to enable ChatGPT-powered recommendations."

    spend_2026 = monthly_category_spend(tx_df, 2026)
    monthly_2026 = monthly_total_expenses(tx_df, 2026)
    if spend_2026.empty:
        return "No 2026 expense data found yet. Import more 2026 months to generate recommendations."

    top_cat_2026 = (
        spend_2026.groupby("category", as_index=False)["spend"]
        .sum()
        .sort_values("spend", ascending=False)
        .head(15)
    )

    recurring_payload = (
        recurring_df[["Merchant", "Category", "Estimated Monthly Cost", "Pattern", "Confidence"]]
        .head(20)
        .to_dict("records")
        if not recurring_df.empty
        else []
    )

    payload = {
        "monthly_goal_reduction_usd": monthly_reduction_goal,
        "user_context": user_context,
        "top_categories_2026": top_cat_2026.to_dict("records"),
        "monthly_totals_2026": monthly_2026.to_dict("records"),
        "recurring_candidates": recurring_payload,
    }

    prompt = (
        "You are a personal finance coach. Based on this spending data, provide:\n"
        "1) A prioritized list of 8 concrete actions to reduce monthly spending.\n"
        "2) Estimated monthly savings per action and confidence.\n"
        "3) A realistic 3-month and 6-month reduction plan.\n"
        "4) Specific subscriptions/recurring charges to review or cancel.\n"
        "Keep it practical and behavior-aware.\n\n"
        f"DATA:\n{json.dumps(payload, default=str)}"
    )

    client = OpenAI(api_key=api_key)
    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt,
        )
        return (response.output_text or "").strip() or "No recommendation text returned."
    except Exception as exc:
        return f"ChatGPT request failed: {exc}"


def render_import_tab() -> int:
    st.subheader("1) Import Monarch Transactions")
    st.caption("Upload one or more monthly Monarch CSV exports. Duplicate rows are skipped automatically.")

    sample_path = "/Users/lwirsing/Downloads/Transactions_2026-02-19T23-55-03.csv"
    if Path(sample_path).exists():
        with st.expander("Preview attached sample CSV schema"):
            try:
                st.dataframe(sample_csv(sample_path).head(10), use_container_width=True)
            except Exception as exc:
                st.warning(f"Could not preview sample file: {exc}")

    uploads = st.file_uploader(
        "Upload Monarch CSV files",
        type=["csv"],
        accept_multiple_files=True,
        key="csv_uploads",
    )

    refresh = 0
    if uploads and st.button("Import Selected Files", type="primary"):
        total_inserted = 0
        total_skipped = 0
        for file in uploads:
            try:
                df = pd.read_csv(file)
                inserted, skipped = import_transactions(df, file.name)
                total_inserted += inserted
                total_skipped += skipped
            except Exception as exc:
                st.error(f"Failed to import {file.name}: {exc}")
        st.success(f"Import complete. Added {total_inserted} rows, skipped {total_skipped} duplicates.")
        refresh = 1

    tx_df = load_all_transactions(refresh)
    c1, c2, c3 = st.columns(3)
    c1.metric("Transactions Stored", f"{len(tx_df):,}")
    c2.metric("Months Loaded", f"{tx_df['month'].nunique() if not tx_df.empty else 0}")
    c3.metric("Categories", f"{tx_df['category'].nunique() if not tx_df.empty else 0}")

    if not tx_df.empty:
        summary = (
            tx_df.groupby("month", as_index=False)
            .agg(
                transactions=("id", "count"),
                expenses=("amount", lambda s: float(s[s < 0].sum() * -1)),
                income=("amount", lambda s: float(s[s > 0].sum())),
            )
            .sort_values("month", ascending=False)
        )
        summary["net"] = summary["income"] - summary["expenses"]
        st.dataframe(summary, use_container_width=True)
        with st.expander("Recent transactions"):
            preview_cols = ["tx_date", "merchant", "category", "amount", "account", "source_file"]
            st.dataframe(
                tx_df.sort_values("tx_date", ascending=False)[preview_cols].head(50),
                use_container_width=True,
            )
    return refresh


def render_monthly_review_tab(tx_df: pd.DataFrame) -> None:
    st.subheader("2) Monthly Budget Review")
    if tx_df.empty:
        st.info("Import at least one CSV file first.")
        return

    month_options = sorted(tx_df["month"].dropna().unique().tolist())
    month = st.selectbox("Review month", options=month_options, index=len(month_options) - 1)
    exclude_transfers = st.toggle("Exclude 'Transfer' category from spend review", value=True)

    review = build_monthly_review(tx_df, month, exclude_transfers)
    if review.empty:
        st.info("No expenses or budgets found for this month.")
        return

    budget_total = float(review["Budget"].sum())
    actual_total = float(review["Actual"].sum())
    variance_total = budget_total - actual_total
    over_count = int((review["Variance"] < 0).sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Budget Total", fmt_usd(budget_total))
    m2.metric("Actual Spend", fmt_usd(actual_total))
    m3.metric("Variance", fmt_usd(variance_total))
    m4.metric("Over-Budget Categories", str(over_count))

    st.dataframe(review, use_container_width=True)

    st.markdown("#### Set Budgets for Next Month")
    target_month = next_month(month)
    st.caption(f"Pre-filled from {month} actuals. Saving writes to budget month {target_month}.")

    current_target = load_budgets(target_month)
    if current_target.empty:
        planner = review[["Category", "Actual"]].copy()
        planner["Budget"] = planner["Actual"]
        planner["Notes"] = ""
    else:
        planner = (
            review[["Category", "Actual"]]
            .merge(
                current_target[["category", "amount", "notes"]].rename(
                    columns={"category": "Category", "amount": "Budget", "notes": "Notes"}
                ),
                on="Category",
                how="outer",
            )
            .fillna({"Actual": 0, "Budget": 0, "Notes": ""})
        )

    edited = st.data_editor(
        planner,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Category": st.column_config.TextColumn(required=True),
            "Actual": st.column_config.NumberColumn("Actual This Month", format="$%.2f", disabled=True),
            "Budget": st.column_config.NumberColumn(f"Budget for {target_month}", format="$%.2f"),
            "Notes": st.column_config.TextColumn(),
        },
        key=f"planner_{target_month}",
    )

    if st.button(f"Save Budgets for {target_month}", type="primary"):
        saved = save_budget_rows(target_month, edited[["Category", "Budget", "Notes"]])
        st.success(f"Saved {saved} budget rows for {target_month}.")


def render_trends_tab(tx_df: pd.DataFrame) -> None:
    st.subheader("3) Category Trends Over Time")
    if tx_df.empty:
        st.info("Import at least one CSV file first.")
        return

    expense_df = tx_df[tx_df["amount"] < 0].copy()
    expense_df["spend"] = expense_df["amount"].abs()

    monthly_category = (
        expense_df.groupby(["month", "category"], as_index=False)["spend"].sum().sort_values("month")
    )
    categories = sorted(monthly_category["category"].dropna().unique().tolist())
    selected = st.multiselect("Category", options=categories, default=categories[: min(3, len(categories))])

    if not selected:
        st.info("Select at least one category.")
        return

    trend = monthly_category[monthly_category["category"].isin(selected)]
    fig = px.line(
        trend,
        x="month",
        y="spend",
        color="category",
        markers=True,
        title="Monthly Spending by Category",
    )
    fig.update_xaxes(title="Month")
    fig.update_yaxes(title="Spend ($)")
    st.plotly_chart(fig, use_container_width=True)

    budget_df = load_budgets()
    if not budget_df.empty:
        budget_plot = budget_df.rename(columns={"month": "Month", "category": "Category", "amount": "Budget"})
        merged = trend.rename(columns={"month": "Month", "category": "Category", "spend": "Actual"}).merge(
            budget_plot,
            on=["Month", "Category"],
            how="left",
        )
        with st.expander("Actual vs budget points"):
            st.dataframe(merged.sort_values(["Month", "Category"]), use_container_width=True)


def render_bills_tab() -> None:
    st.subheader("4) Bills Planner")
    st.caption("Track one-time and recurring bills, then forecast what is coming due.")

    with st.form("add_bill_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Bill name")
        amount = c2.number_input("Amount", min_value=0.0, step=1.0, format="%.2f")
        category = c3.text_input("Category (optional)")

        c4, c5, c6 = st.columns(3)
        recurrence = c4.selectbox("Recurrence", ["one-time", "monthly", "weekly", "yearly"])
        due_date_val = c5.date_input("Due date (required for one-time/weekly/yearly)", value=date.today())
        due_day_val = c6.number_input("Due day (monthly only)", min_value=1, max_value=31, value=1)

        notes = st.text_input("Notes")
        submitted = st.form_submit_button("Add Bill", type="primary")

    if submitted:
        if not name.strip():
            st.error("Bill name is required.")
        elif amount <= 0:
            st.error("Amount must be greater than 0.")
        else:
            add_bill(
                name=name,
                amount=float(amount),
                recurrence=recurrence,
                due_date=due_date_val if recurrence in {"one-time", "weekly", "yearly"} else None,
                due_day=int(due_day_val) if recurrence == "monthly" else None,
                category=category,
                notes=notes,
            )
            st.success("Bill added.")

    bills = list_bills(active_only=False)
    if not bills:
        st.info("No bills yet.")
        return

    bill_rows = pd.DataFrame(
        [
            {
                "id": b.bill_id,
                "Name": b.name,
                "Amount": b.amount,
                "Recurrence": b.recurrence,
                "Due Date": b.due_date,
                "Due Day": b.due_day,
                "Category": b.category,
                "Active": b.active,
                "Notes": b.notes,
            }
            for b in bills
        ]
    )
    st.dataframe(bill_rows.drop(columns=["id"]), use_container_width=True)

    st.markdown("#### Disable / Re-enable Bill")
    id_to_name = {b.bill_id: b.name for b in bills}
    selected_id = st.selectbox("Select bill", options=list(id_to_name.keys()), format_func=lambda x: f"{id_to_name[x]} (#{x})")
    selected_bill = next(b for b in bills if b.bill_id == selected_id)
    action_label = "Disable" if selected_bill.active else "Enable"
    if st.button(f"{action_label} bill"):
        set_bill_active(selected_bill.bill_id, not selected_bill.active)
        st.success(f"Updated bill '{selected_bill.name}'.")

    st.markdown("#### Upcoming Bill Forecast")
    months_ahead = st.slider("Forecast horizon (months)", min_value=1, max_value=12, value=3)
    start = date.today().replace(day=1)
    end_year = start.year + ((start.month - 1 + months_ahead) // 12)
    end_month = (start.month - 1 + months_ahead) % 12 + 1
    end = date(end_year, end_month, 1) - timedelta(days=1)

    active_bills = [b for b in bills if b.active]
    forecast = upcoming_bill_events(active_bills, start=start, end=end)
    if forecast.empty:
        st.info("No upcoming bill events in selected horizon.")
        return

    monthly = forecast.groupby("Month", as_index=False)["Amount"].sum()
    fig = px.bar(monthly, x="Month", y="Amount", title="Planned Bills by Month")
    fig.update_yaxes(title="Planned bills ($)")
    st.plotly_chart(fig, use_container_width=True)

    st.dataframe(forecast[["Date", "Bill", "Category", "Amount"]], use_container_width=True)


def render_dashboard_tab(tx_df: pd.DataFrame) -> None:
    st.subheader("5) Snapshot")
    if tx_df.empty:
        st.info("Import at least one CSV file first.")
        return

    latest_month = sorted(tx_df["month"].dropna().unique().tolist())[-1]
    month_df = tx_df[tx_df["month"] == latest_month]
    income = float(month_df.loc[month_df["amount"] > 0, "amount"].sum())
    expenses = float(month_df.loc[month_df["amount"] < 0, "amount"].sum() * -1)
    net = income - expenses

    budget_df = load_budgets(latest_month)
    budget_total = float(budget_df["amount"].sum()) if not budget_df.empty else 0.0
    remaining = budget_total - expenses

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Month", latest_month)
    c2.metric("Income", fmt_usd(income))
    c3.metric("Expenses", fmt_usd(expenses))
    c4.metric("Net", fmt_usd(net))
    c5.metric("Budget Remaining", fmt_usd(remaining))

    top_spend = (
        month_df[month_df["amount"] < 0]
        .assign(spend=lambda x: x["amount"].abs())
        .groupby("category", as_index=False)["spend"]
        .sum()
        .sort_values("spend", ascending=False)
        .head(10)
    )
    fig = px.bar(top_spend, x="category", y="spend", title=f"Top Spending Categories ({latest_month})")
    fig.update_xaxes(title="Category")
    fig.update_yaxes(title="Spend ($)")
    st.plotly_chart(fig, use_container_width=True)


def render_recurring_tab(tx_df: pd.DataFrame) -> pd.DataFrame:
    st.subheader("6) Recurring Purchases & Subscriptions")
    if tx_df.empty:
        st.info("Import at least one CSV file first.")
        return pd.DataFrame()

    min_occurrences = st.slider("Minimum occurrences to qualify", min_value=3, max_value=12, value=4)
    recurring = detect_recurring_purchases(tx_df, min_occurrences=min_occurrences)
    if recurring.empty:
        st.info("No recurring candidates detected with current settings.")
        return recurring

    est_total = float(recurring["Estimated Monthly Cost"].sum())
    sub_like = recurring[
        recurring["Pattern"].eq("monthly") & recurring["Category"].fillna("").str.contains("internet|service|subscription|cable", case=False)
    ]
    st.metric("Estimated Monthly Recurring Spend (detected)", fmt_usd(est_total))
    st.metric("Potential Subscription Lines", f"{len(sub_like)}")

    st.dataframe(recurring, use_container_width=True)
    fig = px.bar(
        recurring.head(20),
        x="Merchant",
        y="Estimated Monthly Cost",
        color="Pattern",
        title="Top Recurring Costs (Estimated Monthly)",
    )
    st.plotly_chart(fig, use_container_width=True)
    return recurring


def render_savings_ai_tab(tx_df: pd.DataFrame, recurring_df: pd.DataFrame) -> None:
    st.subheader("7) Spending Reduction Opportunities (ChatGPT)")
    if tx_df.empty:
        st.info("Import at least one CSV file first.")
        return

    goal = st.number_input(
        "Target monthly reduction ($)",
        min_value=500.0,
        max_value=10000.0,
        value=2000.0,
        step=100.0,
        format="%.2f",
    )
    context = st.text_area(
        "Context for the advisor (optional)",
        value="I want durable spending cuts without breaking essentials. Prioritize quick wins first.",
        height=90,
    )

    st.markdown("#### Quick Wins (rules-based)")
    spend_2026 = monthly_category_spend(tx_df, 2026)
    if spend_2026.empty:
        st.info("No 2026 expense data available yet.")
    else:
        top_categories = (
            spend_2026.groupby("category", as_index=False)["spend"]
            .mean()
            .rename(columns={"spend": "Avg Monthly Spend 2026"})
            .sort_values("Avg Monthly Spend 2026", ascending=False)
            .head(10)
        )
        top_categories["5% Cut Impact"] = top_categories["Avg Monthly Spend 2026"] * 0.05
        top_categories["10% Cut Impact"] = top_categories["Avg Monthly Spend 2026"] * 0.10
        st.dataframe(top_categories, use_container_width=True)

    st.markdown("#### ChatGPT Recommendations")
    st.caption("Requires OPENAI_API_KEY in your environment.")
    if st.button("Generate AI Plan", type="primary"):
        with st.spinner("Calling ChatGPT..."):
            advice = get_ai_savings_recommendations(tx_df, recurring_df, float(goal), context)
        st.markdown(advice)


def render_2026_tracker_tab(tx_df: pd.DataFrame) -> None:
    st.subheader("8) 2026 Trend Tracker & Reduction Runway")
    if tx_df.empty:
        st.info("Import at least one CSV file first.")
        return

    cat_month = monthly_category_spend(tx_df, 2026)
    if cat_month.empty:
        st.info("No 2026 expense data found yet.")
        return

    monthly = monthly_total_expenses(tx_df, 2026)
    monthly = monthly.sort_values("month").reset_index(drop=True)
    month_options = monthly["month"].tolist()
    baseline_month = st.selectbox("Baseline month", options=month_options, index=0)
    goal_reduction = st.number_input(
        "Monthly reduction goal by end of 2026 ($)",
        min_value=500.0,
        max_value=10000.0,
        value=2000.0,
        step=100.0,
        format="%.2f",
    )

    baseline_spend = float(monthly.loc[monthly["month"] == baseline_month, "total_spend"].iloc[0])
    target_spend = max(0.0, baseline_spend - float(goal_reduction))

    start_idx = month_options.index(baseline_month)
    total_steps = max(1, len(month_options) - 1 - start_idx)
    planned: list[float] = []
    for i, _ in enumerate(month_options):
        if i <= start_idx:
            planned.append(baseline_spend)
        else:
            progress = (i - start_idx) / total_steps
            planned.append(baseline_spend - (baseline_spend - target_spend) * progress)
    monthly["planned_spend"] = planned
    monthly["gap_to_plan"] = monthly["planned_spend"] - monthly["total_spend"]

    latest_actual = float(monthly["total_spend"].iloc[-1])
    reduction_achieved = baseline_spend - latest_actual
    remaining = float(goal_reduction) - reduction_achieved
    m1, m2, m3 = st.columns(3)
    m1.metric("Baseline Monthly Spend", fmt_usd(baseline_spend))
    m2.metric("Latest Monthly Spend", fmt_usd(latest_actual))
    m3.metric("Reduction Remaining", fmt_usd(max(0.0, remaining)))

    runway = monthly.melt(
        id_vars=["month"],
        value_vars=["total_spend", "planned_spend"],
        var_name="Series",
        value_name="Amount",
    )
    fig_runway = px.line(
        runway,
        x="month",
        y="Amount",
        color="Series",
        markers=True,
        title="2026 Monthly Spend: Actual vs Reduction Runway",
    )
    st.plotly_chart(fig_runway, use_container_width=True)

    categories = sorted(cat_month["category"].dropna().unique().tolist())
    default_cats = categories[: min(5, len(categories))]
    selected = st.multiselect("Categories to track", options=categories, default=default_cats)
    if selected:
        filtered = cat_month[cat_month["category"].isin(selected)]
        fig_cat = px.line(
            filtered,
            x="month",
            y="spend",
            color="category",
            markers=True,
            title="2026 Category Spend by Month",
        )
        st.plotly_chart(fig_cat, use_container_width=True)

    pivot = cat_month.pivot(index="category", columns="month", values="spend").fillna(0.0)
    st.dataframe(pivot, use_container_width=True)


def main() -> None:
    init_db()
    st.title("Monarch Budget & Bill Planner")
    st.caption(
        "Import monthly Monarch CSV files, run category-by-category budget reviews, set next-month budgets, and forecast bills."
    )

    tab_names = [
        "Import",
        "Monthly Review",
        "Trends",
        "Bills",
        "Snapshot",
        "Recurring",
        "Savings AI",
        "2026 Tracker",
    ]
    t1, t2, t3, t4, t5, t6, t7, t8 = st.tabs(tab_names)

    with t1:
        refresh = render_import_tab()

    tx_df = load_all_transactions(refresh)

    with t2:
        render_monthly_review_tab(tx_df)

    with t3:
        render_trends_tab(tx_df)

    with t4:
        render_bills_tab()

    with t5:
        render_dashboard_tab(tx_df)

    recurring_df = pd.DataFrame()
    with t6:
        recurring_df = render_recurring_tab(tx_df)

    with t7:
        render_savings_ai_tab(tx_df, recurring_df)

    with t8:
        render_2026_tracker_tab(tx_df)


if __name__ == "__main__":
    main()
