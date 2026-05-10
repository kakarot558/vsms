from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from database.db import get_db
from functools import wraps
from datetime import date, timedelta

inventory_bp = Blueprint('inventory', __name__)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get('role') not in roles:
                flash('Access denied.', 'danger')
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return decorated
    return decorator

def log_audit(user_id, action, module, description):
    try:
        conn = get_db()
        conn.execute("INSERT INTO audit_logs (user_id, action, module, description) VALUES (?,?,?,?)",
                     (user_id, action, module, description))
        conn.commit()
        conn.close()
    except: pass

@inventory_bp.route('/inventory')
@login_required
@role_required('admin', 'inventory_manager')
def index():
    conn = get_db()
    today = date.today().isoformat()
    warn_date = (date.today() + timedelta(days=30)).isoformat()
    search = request.args.get('search', '')
    filter_type = request.args.get('filter', '')

    query = """SELECT p.*, s.name as supplier_name,
        (SELECT COUNT(*) FROM product_batches WHERE product_id=p.product_id AND remaining_quantity>0) as batch_count
        FROM products p LEFT JOIN suppliers s ON p.supplier_id=s.supplier_id WHERE p.is_active=1"""
    params = []
    if search:
        query += " AND (p.product_name LIKE ? OR p.barcode LIKE ?)"
        params += [f'%{search}%', f'%{search}%']
    if filter_type == 'low':
        query += " AND p.stock <= p.low_stock_threshold"
    elif filter_type == 'expiring':
        query += """ AND p.product_id IN (
            SELECT product_id FROM product_batches 
            WHERE expiration_date BETWEEN ? AND ? AND remaining_quantity>0)"""
        params += [today, warn_date]
    query += " ORDER BY p.product_name"
    products = conn.execute(query, params).fetchall()

    # Expiring batches
    expiring_batches = conn.execute("""
        SELECT pb.*, p.product_name, p.unit, s.name as supplier_name
        FROM product_batches pb
        JOIN products p ON pb.product_id=p.product_id
        LEFT JOIN suppliers s ON pb.supplier_id=s.supplier_id
        WHERE pb.expiration_date IS NOT NULL
          AND pb.expiration_date BETWEEN ? AND ?
          AND pb.remaining_quantity > 0
        ORDER BY pb.expiration_date ASC
    """, (today, warn_date)).fetchall()

    expired_batches = conn.execute("""
        SELECT pb.*, p.product_name, p.unit
        FROM product_batches pb
        JOIN products p ON pb.product_id=p.product_id
        WHERE pb.expiration_date < ? AND pb.remaining_quantity > 0
        ORDER BY pb.expiration_date DESC
    """, (today,)).fetchall()

    recent_logs = conn.execute("""
        SELECT il.*, p.product_name, u.full_name as user_name
        FROM inventory_logs il
        JOIN products p ON il.product_id=p.product_id
        JOIN users u ON il.user_id=u.user_id
        ORDER BY il.created_at DESC LIMIT 30
    """).fetchall()

    suppliers = conn.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()
    conn.close()

    return render_template('inventory.html',
        products=products, expiring_batches=expiring_batches,
        expired_batches=expired_batches, recent_logs=recent_logs,
        suppliers=suppliers, search=search, filter_type=filter_type)

@inventory_bp.route('/inventory/stock-in', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def stock_in():
    data = request.form
    product_id = int(data['product_id'])
    qty = int(data['quantity'])
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    old_stock = product['stock']
    new_stock = old_stock + qty

    conn.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?",
                 (new_stock, product_id))
    batch_num = data.get('batch_number', f"BATCH-{date.today().strftime('%Y%m%d')}-{product_id}")
    conn.execute("""INSERT INTO product_batches 
        (product_id, batch_number, expiration_date, quantity, remaining_quantity, supplier_id, purchase_price, notes)
        VALUES (?,?,?,?,?,?,?,?)""",
        (product_id, batch_num, data.get('expiration_date') or None,
         qty, qty, data.get('supplier_id') or None,
         float(data.get('purchase_price', 0)) or None, data.get('notes', '')))
    conn.execute("""INSERT INTO inventory_logs 
        (product_id, user_id, action, quantity_change, quantity_before, quantity_after, notes)
        VALUES (?,?,'stock_in',?,?,?,?)""",
        (product_id, session['user_id'], qty, old_stock, new_stock, data.get('notes', 'Stock in')))
    conn.commit()
    conn.close()
    log_audit(session['user_id'], 'STOCK_IN', 'Inventory',
              f"Stock in: {product['product_name']} +{qty} units")
    flash(f'Stock added successfully! New stock: {new_stock}', 'success')
    return redirect(url_for('inventory.index'))

@inventory_bp.route('/inventory/stock-adjustment', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def stock_adjustment():
    data = request.form
    product_id = int(data['product_id'])
    new_qty = int(data['new_quantity'])
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    old_stock = product['stock']
    change = new_qty - old_stock
    conn.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?",
                 (new_qty, product_id))
    conn.execute("""INSERT INTO inventory_logs 
        (product_id, user_id, action, quantity_change, quantity_before, quantity_after, notes)
        VALUES (?,?,'adjustment',?,?,?,?)""",
        (product_id, session['user_id'], change, old_stock, new_qty,
         data.get('notes', 'Manual adjustment')))
    conn.commit()
    conn.close()
    log_audit(session['user_id'], 'ADJUSTMENT', 'Inventory',
              f"Adjusted: {product['product_name']} from {old_stock} to {new_qty}")
    flash('Stock adjusted successfully!', 'success')
    return redirect(url_for('inventory.index'))

@inventory_bp.route('/inventory/add-batch', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def add_batch():
    data = request.form
    product_id = int(data['product_id'])
    qty = int(data['quantity'])
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()
    old_stock = product['stock']
    new_stock = old_stock + qty
    conn.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?",
                 (new_stock, product_id))
    conn.execute("""INSERT INTO product_batches 
        (product_id, batch_number, expiration_date, quantity, remaining_quantity, supplier_id, purchase_price, notes)
        VALUES (?,?,?,?,?,?,?,?)""",
        (product_id, data['batch_number'], data.get('expiration_date') or None,
         qty, qty, data.get('supplier_id') or None,
         float(data.get('purchase_price', 0)) or None, data.get('notes', '')))
    conn.execute("""INSERT INTO inventory_logs 
        (product_id, user_id, action, quantity_change, quantity_before, quantity_after, notes)
        VALUES (?,?,'stock_in',?,?,?,?)""",
        (product_id, session['user_id'], qty, old_stock, new_stock, f"Batch {data['batch_number']} received"))
    conn.commit()
    conn.close()
    flash('Batch added successfully!', 'success')
    return redirect(url_for('inventory.index'))

@inventory_bp.route('/inventory/remove-expired', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def remove_expired():
    """Remove expired/spoiled stock from a batch and deduct from product stock."""
    data       = request.get_json() or {}
    batch_id   = int(data.get('batch_id', 0))
    product_id = int(data.get('product_id', 0))
    qty        = int(data.get('quantity', 0))
    reason     = data.get('reason', 'expired')

    if qty < 1 or not batch_id or not product_id:
        return jsonify({'success': False, 'message': 'Invalid input'}), 400

    conn = get_db()
    try:
        batch   = conn.execute("SELECT * FROM product_batches WHERE batch_id=?", (batch_id,)).fetchone()
        product = conn.execute("SELECT * FROM products WHERE product_id=?", (product_id,)).fetchone()

        if not batch or not product:
            conn.close()
            return jsonify({'success': False, 'message': 'Batch or product not found'}), 404

        actual_qty = min(qty, batch['remaining_quantity'])  # never remove more than available

        old_stock   = product['stock']
        new_stock   = max(0, old_stock - actual_qty)
        new_batch_q = max(0, batch['remaining_quantity'] - actual_qty)

        # Update batch remaining quantity
        conn.execute(
            "UPDATE product_batches SET remaining_quantity=? WHERE batch_id=?",
            (new_batch_q, batch_id)
        )

        # Update product stock
        conn.execute(
            "UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?",
            (new_stock, product_id)
        )

        # Log the removal
        action_label = reason if reason in ('expired', 'spoiled', 'disposed') else 'adjustment'
        conn.execute("""
            INSERT INTO inventory_logs
              (product_id, user_id, action, quantity_change, quantity_before, quantity_after, notes)
            VALUES (?,?,'adjustment',?,?,?,?)
        """, (product_id, session['user_id'], -actual_qty, old_stock, new_stock,
              f"{reason.title()} stock removal — batch {batch['batch_number']}"))

        # Log to product_movements
        conn.execute("""
            INSERT INTO product_movements
              (product_id, user_id, move_type, quantity, quantity_before, quantity_after,
               reference_type, batch_id, notes)
            VALUES (?,?,'expired_removal',?,?,?,'inventory',?,?)
        """, (product_id, session['user_id'], -actual_qty, old_stock, new_stock,
              batch_id, f"{reason.title()} — removed {actual_qty} units"))

        conn.commit()
        conn.close()
        return jsonify({'success': True, 'remaining': new_batch_q, 'new_stock': new_stock})

    except Exception as e:
        try: conn.rollback()
        except: pass
        conn.close()
        return jsonify({'success': False, 'message': str(e)}), 500
