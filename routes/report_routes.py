from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash
from database.db import get_db
from functools import wraps
from datetime import date, timedelta

report_bp = Blueprint('reports', __name__)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def dec(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get('role') not in roles:
                flash('Access denied.', 'danger')
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return decorated
    return dec

# ── Helper: fetch report data for a date range ─────────────────────────────────
def _fetch_report_data(conn, start_date, end_date):
    """Returns sales + inventory summary for a given date range (ISO strings)."""
    # Sales summary
    sales = conn.execute("""
        SELECT COALESCE(SUM(total_amount),0) as revenue,
               COALESCE(SUM(discount_amount),0) as discounts,
               COUNT(*) as txn_count
        FROM transactions
        WHERE DATE(created_at) BETWEEN ? AND ? AND status='completed'
    """, (start_date, end_date)).fetchone()

    profit = conn.execute("""
        SELECT COALESCE(SUM(ti.total_price - (ti.quantity * p.cost_price)),0)
        FROM transaction_items ti
        JOIN transactions t ON ti.transaction_id=t.transaction_id
        JOIN products p ON ti.product_id=p.product_id
        WHERE DATE(t.created_at) BETWEEN ? AND ? AND t.status='completed'
    """, (start_date, end_date)).fetchone()[0]

    # Top sellers for period
    top_sellers = conn.execute("""
        SELECT p.product_name, p.category, p.unit,
               SUM(ti.quantity) as total_sold,
               SUM(ti.total_price) as revenue,
               SUM(ti.total_price - (ti.quantity * p.cost_price)) as profit
        FROM transaction_items ti
        JOIN products p ON ti.product_id=p.product_id
        JOIN transactions t ON ti.transaction_id=t.transaction_id
        WHERE DATE(t.created_at) BETWEEN ? AND ? AND t.status='completed'
        GROUP BY p.product_id ORDER BY total_sold DESC LIMIT 10
    """, (start_date, end_date)).fetchall()

    # Category breakdown
    category_sales = conn.execute("""
        SELECT p.category, SUM(ti.total_price) as revenue, SUM(ti.quantity) as units_sold
        FROM transaction_items ti
        JOIN products p ON ti.product_id=p.product_id
        JOIN transactions t ON ti.transaction_id=t.transaction_id
        WHERE DATE(t.created_at) BETWEEN ? AND ? AND t.status='completed'
        GROUP BY p.category ORDER BY revenue DESC
    """, (start_date, end_date)).fetchall()

    # Daily breakdown within range
    daily_breakdown = []
    from datetime import date as _date
    d = _date.fromisoformat(start_date)
    end = _date.fromisoformat(end_date)
    while d <= end:
        row = conn.execute("""
            SELECT COALESCE(SUM(total_amount),0) as rev, COUNT(*) as cnt
            FROM transactions WHERE DATE(created_at)=? AND status='completed'
        """, (d.isoformat(),)).fetchone()
        daily_breakdown.append({'date': d.isoformat(), 'revenue': round(row['rev'], 2), 'count': row['cnt']})
        d += timedelta(days=1)

    # Inventory snapshot
    low_stock = conn.execute("""
        SELECT product_name, category, stock, low_stock_threshold, unit
        FROM products WHERE is_active=1 AND stock <= low_stock_threshold ORDER BY stock ASC
    """).fetchall()

    stock_received = conn.execute("""
        SELECT COALESCE(SUM(quantity_change),0) FROM inventory_logs
        WHERE DATE(created_at) BETWEEN ? AND ? AND action='stock_in'
    """, (start_date, end_date)).fetchone()[0]

    stock_out = conn.execute("""
        SELECT COALESCE(SUM(ABS(quantity_change)),0) FROM inventory_logs
        WHERE DATE(created_at) BETWEEN ? AND ? AND action='sale'
    """, (start_date, end_date)).fetchone()[0]

    return {
        'revenue':        round(sales['revenue'], 2),
        'discounts':      round(sales['discounts'], 2),
        'txn_count':      sales['txn_count'],
        'profit':         round(profit, 2),
        'top_sellers':    top_sellers,
        'category_sales': category_sales,
        'daily_breakdown': daily_breakdown,
        'low_stock':      low_stock,
        'stock_received': stock_received or 0,
        'stock_out':      stock_out or 0,
    }

# ── Main reports dashboard (unchanged) ────────────────────────────────────────
@report_bp.route('/reports')
@login_required
@role_required('admin', 'inventory_manager')
def index():
    conn = get_db()
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    year_start = today.replace(month=1, day=1).isoformat()

    summary = {
        'today_revenue': conn.execute("""SELECT COALESCE(SUM(total_amount),0) FROM transactions 
            WHERE DATE(created_at)=? AND status='completed'""", (today.isoformat(),)).fetchone()[0],
        'monthly_revenue': conn.execute("""SELECT COALESCE(SUM(total_amount),0) FROM transactions 
            WHERE DATE(created_at)>=? AND status='completed'""", (month_start,)).fetchone()[0],
        'yearly_revenue': conn.execute("""SELECT COALESCE(SUM(total_amount),0) FROM transactions 
            WHERE DATE(created_at)>=? AND status='completed'""", (year_start,)).fetchone()[0],
        'total_transactions': conn.execute("""SELECT COUNT(*) FROM transactions WHERE status='completed'""").fetchone()[0],
        'monthly_profit': conn.execute("""
            SELECT COALESCE(SUM(ti.total_price - (ti.quantity * p.cost_price)),0)
            FROM transaction_items ti
            JOIN transactions t ON ti.transaction_id=t.transaction_id
            JOIN products p ON ti.product_id=p.product_id
            WHERE DATE(t.created_at)>=? AND t.status='completed'""", (month_start,)).fetchone()[0],
    }

    best_sellers = conn.execute("""
        SELECT p.product_name, p.category, p.unit,
               SUM(ti.quantity) as total_sold,
               SUM(ti.total_price) as revenue,
               SUM(ti.total_price - (ti.quantity * p.cost_price)) as profit
        FROM transaction_items ti
        JOIN products p ON ti.product_id=p.product_id
        JOIN transactions t ON ti.transaction_id=t.transaction_id
        WHERE t.status='completed'
        GROUP BY p.product_id ORDER BY total_sold DESC LIMIT 10
    """).fetchall()

    monthly_data = []
    for i in range(11, -1, -1):
        d = today.replace(day=1) - timedelta(days=i*30)
        m_start = d.replace(day=1).isoformat()
        if d.month == 12:
            m_end = d.replace(year=d.year+1, month=1, day=1) - timedelta(days=1)
        else:
            m_end = d.replace(month=d.month+1, day=1) - timedelta(days=1)
        m_end = m_end.isoformat()
        row = conn.execute("""
            SELECT COALESCE(SUM(total_amount),0) as revenue, COUNT(*) as txn_count
            FROM transactions WHERE DATE(created_at) BETWEEN ? AND ? AND status='completed'
        """, (m_start, m_end)).fetchone()
        monthly_data.append({'month': d.strftime('%b %Y'), 'revenue': round(row['revenue'], 2), 'count': row['txn_count']})

    category_sales = conn.execute("""
        SELECT p.category, SUM(ti.total_price) as revenue, SUM(ti.quantity) as units_sold
        FROM transaction_items ti
        JOIN products p ON ti.product_id=p.product_id
        JOIN transactions t ON ti.transaction_id=t.transaction_id
        WHERE t.status='completed'
        GROUP BY p.category ORDER BY revenue DESC
    """).fetchall()

    daily_data = []
    for i in range(29, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        amt = conn.execute("""SELECT COALESCE(SUM(total_amount),0) FROM transactions 
            WHERE DATE(created_at)=? AND status='completed'""", (d,)).fetchone()[0]
        daily_data.append({'date': d[5:], 'amount': round(amt, 2)})

    conn.close()
    return render_template('reports.html',
        summary=summary, best_sellers=best_sellers,
        monthly_data=monthly_data, category_sales=category_sales, daily_data=daily_data)

# ── DAILY REPORT ───────────────────────────────────────────────────────────────
@report_bp.route('/reports/daily')
@login_required
@role_required('admin', 'inventory_manager')
def daily_report():
    conn = get_db()
    # Allow ?date= param for viewing past days
    target = request.args.get('date', date.today().isoformat())
    try:
        target_date = date.fromisoformat(target)
    except ValueError:
        target_date = date.today()
    data = _fetch_report_data(conn, target_date.isoformat(), target_date.isoformat())
    conn.close()
    return render_template('report_period.html',
        period='Daily', period_label=target_date.strftime('%B %d, %Y'),
        start=target_date.isoformat(), end=target_date.isoformat(),
        data=data, target_date=target_date.isoformat())

# ── WEEKLY REPORT ──────────────────────────────────────────────────────────────
@report_bp.route('/reports/weekly')
@login_required
@role_required('admin', 'inventory_manager')
def weekly_report():
    conn = get_db()
    today = date.today()
    # Start of this week (Monday)
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    # Allow ?week= offset (0=current, 1=last week, etc.)
    offset = int(request.args.get('offset', 0))
    week_start -= timedelta(weeks=offset)
    week_end -= timedelta(weeks=offset)

    data = _fetch_report_data(conn, week_start.isoformat(), week_end.isoformat())
    conn.close()
    label = f"{week_start.strftime('%b %d')} – {week_end.strftime('%b %d, %Y')}"
    return render_template('report_period.html',
        period='Weekly', period_label=label,
        start=week_start.isoformat(), end=week_end.isoformat(),
        data=data, offset=offset)

# ── MONTHLY REPORT ─────────────────────────────────────────────────────────────
@report_bp.route('/reports/monthly')
@login_required
@role_required('admin', 'inventory_manager')
def monthly_report():
    conn = get_db()
    today = date.today()
    # Allow ?offset= months back
    offset = int(request.args.get('offset', 0))
    # Calculate target month
    month = today.month - offset
    year = today.year
    while month < 1:
        month += 12
        year -= 1
    month_start = date(year, month, 1)
    if month == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, month + 1, 1) - timedelta(days=1)

    data = _fetch_report_data(conn, month_start.isoformat(), month_end.isoformat())
    conn.close()
    label = month_start.strftime('%B %Y')
    return render_template('report_period.html',
        period='Monthly', period_label=label,
        start=month_start.isoformat(), end=month_end.isoformat(),
        data=data, offset=offset)

# ── AUDIT LOG API ──────────────────────────────────────────────────────────────
@report_bp.route('/api/reports/audit-log')
@login_required
@role_required('admin', 'inventory_manager')
def audit_log():
    conn = get_db()
    page = int(request.args.get('page', 1))
    per_page = 30
    offset = (page - 1) * per_page
    logs = conn.execute("""
        SELECT al.*, u.full_name as user_name
        FROM audit_logs al LEFT JOIN users u ON al.user_id=u.user_id
        ORDER BY al.created_at DESC LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
    conn.close()
    return jsonify({'logs': [dict(l) for l in logs], 'total': total})
