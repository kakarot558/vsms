from flask import Flask, redirect, url_for, session
from datetime import date as _date
import sqlite3 as _sqlite3
import os

app = Flask(__name__)
app.secret_key = 'vsms_secret_key_2024_veterinary'

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# ── Custom Jinja2 filters ─────────────────────────────────────────────────────
@app.template_filter('to_date')
def to_date_filter(s):
    try:
        return _date.fromisoformat(str(s))
    except Exception:
        return _date.today()

@app.template_filter('todict')
def todict_filter(row):
    """Convert sqlite3.Row → plain dict so tojson works in templates."""
    if isinstance(row, _sqlite3.Row):
        return dict(row)
    return row

# ── Global template variables ─────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    from datetime import timedelta, datetime
    return {
        'today': _date.today(),
        'today_str': _date.today().isoformat(),
        'timedelta': timedelta,
        'now': datetime.now,
    }

# ── Blueprints ────────────────────────────────────────────────────────────────
from routes.auth_routes import auth_bp
from routes.dashboard_routes import dashboard_bp
from routes.product_routes import product_bp
from routes.inventory_routes import inventory_bp
from routes.pos_routes import pos_bp
from routes.supplier_routes import supplier_bp
from routes.report_routes import report_bp
from routes.alert_routes import alert_bp
from routes.user_routes import user_bp
from routes.po_routes import po_bp
from routes.backup_routes import backup_bp

app.register_blueprint(auth_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(product_bp)
app.register_blueprint(inventory_bp)
app.register_blueprint(pos_bp)
app.register_blueprint(supplier_bp)
app.register_blueprint(report_bp)
app.register_blueprint(alert_bp)
app.register_blueprint(user_bp)
app.register_blueprint(po_bp)
app.register_blueprint(backup_bp)

# ── Root route ────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard.index'))
    return redirect(url_for('auth.login'))

# ── DB init — runs on every startup (safe: CREATE TABLE IF NOT EXISTS) ─────────
from database.db import init_db
init_db()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    app.run(debug=True)
