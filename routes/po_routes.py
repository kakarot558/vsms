from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from database.db import get_db
from functools import wraps
from datetime import date, datetime
import random, string

po_bp = Blueprint('purchase_orders', __name__)

def login_required(f):
    @wraps(f)
    def d(*a, **kw):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*a, **kw)
    return d

def role_required(*roles):
    def dec(f):
        @wraps(f)
        def d(*a, **kw):
            if session.get('role') not in roles:
                flash('Access denied.', 'danger')
                return redirect(url_for('dashboard.index'))
            return f(*a, **kw)
        return d
    return dec

def gen_po_number():
    return f"PO-{date.today().strftime('%Y%m%d')}-{''.join(random.choices(string.digits, k=4))}"

def audit(user_id, action, desc):
    try:
        conn = get_db()
        conn.execute("INSERT INTO audit_logs (user_id, action, module, description) VALUES (?,?,?,?)",
                     (user_id, action, 'Purchase Orders', desc))
        conn.commit(); conn.close()
    except: pass

# ── LIST ──────────────────────────────────────────────────────────────────────
@po_bp.route('/purchase-orders')
@login_required
@role_required('admin', 'inventory_manager')
def index():
    conn = get_db()
    status_filter = request.args.get('status', '')
    supplier_filter = request.args.get('supplier', '')
    search = request.args.get('search', '')

    q = """SELECT po.*, s.name as supplier_name,
               u.full_name as created_by_name,
               (SELECT COUNT(*) FROM purchase_order_items WHERE po_id=po.po_id) as item_count
           FROM purchase_orders po
           JOIN suppliers s ON po.supplier_id=s.supplier_id
           JOIN users u ON po.created_by=u.user_id
           WHERE 1=1"""
    params = []
    if status_filter:
        q += " AND po.status=?"; params.append(status_filter)
    if supplier_filter:
        q += " AND po.supplier_id=?"; params.append(supplier_filter)
    if search:
        q += " AND (po.po_number LIKE ? OR s.name LIKE ?)"; params += [f'%{search}%']*2
    q += " ORDER BY po.created_at DESC"

    orders = conn.execute(q, params).fetchall()
    suppliers = conn.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()
    products  = [dict(r) for r in conn.execute("""SELECT product_id, product_name, unit, cost_price
                                FROM products WHERE is_active=1 ORDER BY product_name""").fetchall()]

    stats = {
        'pending':  conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status='pending'").fetchone()[0],
        'arrived':  conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status='arrived'").fetchone()[0],
        'approved': conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE status='approved'").fetchone()[0],
        'total_value': conn.execute("SELECT COALESCE(SUM(total_amount),0) FROM purchase_orders WHERE status!='cancelled'").fetchone()[0],
    }
    conn.close()
    return render_template('purchase_orders.html',
        orders=orders, suppliers=suppliers, products=products,
        stats=stats, status_filter=status_filter,
        supplier_filter=supplier_filter, search=search)

# ── CREATE ─────────────────────────────────────────────────────────────────────
@po_bp.route('/purchase-orders/create', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def create():
    data = request.form
    supplier_id   = int(data['supplier_id'])
    expected_date = data.get('expected_date') or None
    notes         = data.get('notes', '')

    product_ids   = request.form.getlist('product_id[]')
    quantities    = request.form.getlist('quantity[]')
    unit_costs    = request.form.getlist('unit_cost[]')
    exp_dates     = request.form.getlist('exp_date[]')
    batch_nums    = request.form.getlist('batch_number[]')

    if not product_ids:
        flash('Add at least one product to the order.', 'danger')
        return redirect(url_for('purchase_orders.index'))

    conn = get_db()
    po_number = gen_po_number()
    while conn.execute("SELECT 1 FROM purchase_orders WHERE po_number=?", (po_number,)).fetchone():
        po_number = gen_po_number()

    total = sum(float(q or 0) * float(c or 0) for q, c in zip(quantities, unit_costs))

    conn.execute("""INSERT INTO purchase_orders
        (po_number, supplier_id, created_by, expected_date, notes, total_amount, status)
        VALUES (?,?,?,?,?,?,'pending')""",
        (po_number, supplier_id, session['user_id'], expected_date, notes, total))
    po_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for pid, qty, cost, exp, batch in zip(product_ids, quantities, unit_costs, exp_dates, batch_nums):
        if not pid or not qty: continue
        p = conn.execute("SELECT product_name FROM products WHERE product_id=?", (pid,)).fetchone()
        if not p: continue
        conn.execute("""INSERT INTO purchase_order_items
            (po_id, product_id, product_name, quantity_ordered, quantity_received, unit_cost, expiration_date, batch_number)
            VALUES (?,?,?,?,0,?,?,?)""",
            (po_id, int(pid), p['product_name'], int(qty), float(cost or 0),
             exp or None, batch or None))

    conn.commit(); conn.close()
    audit(session['user_id'], 'CREATE_PO', f"Created PO {po_number} for supplier {supplier_id}")
    flash(f'Purchase Order {po_number} created successfully!', 'success')
    return redirect(url_for('purchase_orders.index'))

# ── DETAIL ─────────────────────────────────────────────────────────────────────
@po_bp.route('/purchase-orders/<int:po_id>')
@login_required
def detail(po_id):
    conn = get_db()
    po = conn.execute("""SELECT po.*, s.name as supplier_name, s.phone as supplier_phone,
                             s.email as supplier_email,
                             u.full_name as created_by_name,
                             a.full_name as approved_by_name
                         FROM purchase_orders po
                         JOIN suppliers s ON po.supplier_id=s.supplier_id
                         JOIN users u ON po.created_by=u.user_id
                         LEFT JOIN users a ON po.approved_by=a.user_id
                         WHERE po.po_id=?""", (po_id,)).fetchone()
    if not po:
        flash('Purchase order not found.', 'danger')
        return redirect(url_for('purchase_orders.index'))
    items = conn.execute("""SELECT poi.*, p.unit, p.stock as current_stock
                            FROM purchase_order_items poi
                            JOIN products p ON poi.product_id=p.product_id
                            WHERE poi.po_id=?""", (po_id,)).fetchall()
    conn.close()
    return render_template('po_detail.html', po=po, items=items)

# ── MARK ARRIVED (still pending approval) ──────────────────────────────────────
@po_bp.route('/purchase-orders/<int:po_id>/mark-arrived', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def mark_arrived(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
    if not po or po['status'] not in ('pending', 'partial'):
        flash('Cannot mark this order as arrived.', 'danger')
        conn.close()
        return redirect(url_for('purchase_orders.detail', po_id=po_id))

    arrived_date = request.form.get('arrived_date') or date.today().isoformat()
    notes = request.form.get('notes', '')

    # Update received quantities from form
    items = conn.execute("SELECT * FROM purchase_order_items WHERE po_id=?", (po_id,)).fetchall()
    all_received = True
    for item in items:
        recv_qty = int(request.form.get(f'recv_{item["poi_id"]}', 0))
        conn.execute("UPDATE purchase_order_items SET quantity_received=? WHERE poi_id=?",
                     (recv_qty, item['poi_id']))
        if recv_qty < item['quantity_ordered']:
            all_received = False

    new_status = 'arrived' if all_received else 'partial'
    conn.execute("""UPDATE purchase_orders SET status=?, arrived_date=?,
                    notes=CASE WHEN notes IS NULL OR notes='' THEN ? ELSE notes||' | '||? END,
                    updated_at=CURRENT_TIMESTAMP WHERE po_id=?""",
                 (new_status, arrived_date, notes, notes, po_id))
    conn.commit(); conn.close()
    audit(session['user_id'], 'PO_ARRIVED', f"PO {po['po_number']} marked as {new_status}")
    flash(f'Order marked as {"fully arrived" if all_received else "partially arrived"}. Ready for approval.', 'success')
    return redirect(url_for('purchase_orders.detail', po_id=po_id))

# ── APPROVE (pushes to inventory) ──────────────────────────────────────────────
@po_bp.route('/purchase-orders/<int:po_id>/approve', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def approve(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
    if not po or po['status'] not in ('arrived', 'partial'):
        flash('Only arrived orders can be approved.', 'danger')
        conn.close()
        return redirect(url_for('purchase_orders.detail', po_id=po_id))

    items = conn.execute("""SELECT poi.*, p.stock as current_stock
                            FROM purchase_order_items poi
                            JOIN products p ON poi.product_id=p.product_id
                            WHERE poi.po_id=?""", (po_id,)).fetchall()
    try:
        for item in items:
            recv = item['quantity_received']
            if recv <= 0:
                continue
            pid = item['product_id']
            old_stock = item['current_stock']
            new_stock = old_stock + recv

            # Update product stock
            conn.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?",
                         (new_stock, pid))

            # Create batch record
            batch_num = item['batch_number'] or f"PO-{po['po_number']}-{pid}"
            conn.execute("""INSERT INTO product_batches
                (product_id, batch_number, expiration_date, quantity, remaining_quantity, supplier_id, purchase_price, notes)
                VALUES (?,?,?,?,?,?,?,?)""",
                (pid, batch_num, item['expiration_date'], recv, recv,
                 po['supplier_id'], item['unit_cost'], f"From PO {po['po_number']}"))
            batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # Inventory log
            conn.execute("""INSERT INTO inventory_logs
                (product_id, user_id, action, quantity_change, quantity_before, quantity_after, reference_id, notes)
                VALUES (?,?,'stock_in',?,?,?,?,?)""",
                (pid, session['user_id'], recv, old_stock, new_stock,
                 po['po_number'], f"Approved from PO {po['po_number']}"))

            # Product movement record
            conn.execute("""INSERT INTO product_movements
                (product_id, user_id, move_type, quantity, quantity_before, quantity_after,
                 unit_cost, reference_type, reference_id, batch_id, notes)
                VALUES (?,?,'stock_in',?,?,?,?,'purchase_order',?,?,?)""",
                (pid, session['user_id'], recv, old_stock, new_stock,
                 item['unit_cost'], po['po_number'], batch_id,
                 f"Received via PO {po['po_number']}"))

        conn.execute("""UPDATE purchase_orders SET status='approved', approved_by=?,
                        updated_at=CURRENT_TIMESTAMP WHERE po_id=?""",
                     (session['user_id'], po_id))
        conn.commit()
        audit(session['user_id'], 'PO_APPROVED', f"PO {po['po_number']} approved — stock updated")
        flash(f'Purchase Order {po["po_number"]} approved! Stock has been updated.', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error approving order: {str(e)}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('purchase_orders.detail', po_id=po_id))

# ── CANCEL ─────────────────────────────────────────────────────────────────────
@po_bp.route('/purchase-orders/<int:po_id>/cancel', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def cancel(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
    if po and po['status'] not in ('approved',):
        conn.execute("UPDATE purchase_orders SET status='cancelled', updated_at=CURRENT_TIMESTAMP WHERE po_id=?", (po_id,))
        conn.commit()
        audit(session['user_id'], 'PO_CANCELLED', f"PO {po['po_number']} cancelled")
        flash('Purchase order cancelled.', 'warning')
    conn.close()
    return redirect(url_for('purchase_orders.index'))

# ── PRODUCT MOVEMENT HISTORY API ───────────────────────────────────────────────
@po_bp.route('/api/products/<int:pid>/movements')
@login_required
def product_movements(pid):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE product_id=?", (pid,)).fetchone()
    if not product:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    moves = conn.execute("""
        SELECT pm.*, u.full_name as user_name
        FROM product_movements pm
        JOIN users u ON pm.user_id=u.user_id
        WHERE pm.product_id=?
        ORDER BY pm.created_at DESC LIMIT 100
    """, (pid,)).fetchall()
    # Also get inventory_logs for legacy moves
    logs = conn.execute("""
        SELECT il.*, u.full_name as user_name
        FROM inventory_logs il
        JOIN users u ON il.user_id=u.user_id
        WHERE il.product_id=?
        ORDER BY il.created_at DESC LIMIT 50
    """, (pid,)).fetchall()
    conn.close()
    return jsonify({
        'product': dict(product),
        'movements': [dict(m) for m in moves],
        'logs': [dict(l) for l in logs]
    })

# ── MANUAL DEDUCTION (damage / adjustment) ────────────────────────────────────
@po_bp.route('/api/inventory/manual-deduction', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def manual_deduction():
    data = request.get_json()
    pid       = int(data['product_id'])
    qty       = int(data['quantity'])
    move_type = data.get('move_type', 'adjustment')  # damaged / returned / adjustment
    notes     = data.get('notes', '')
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE product_id=? AND is_active=1", (pid,)).fetchone()
    if not product:
        conn.close(); return jsonify({'success': False, 'message': 'Product not found'}), 404
    if qty > product['stock']:
        conn.close(); return jsonify({'success': False, 'message': f'Cannot deduct {qty} — only {product["stock"]} in stock'}), 400
    old_stock = product['stock']
    new_stock = old_stock - qty
    try:
        conn.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?", (new_stock, pid))
        conn.execute("""INSERT INTO inventory_logs
            (product_id, user_id, action, quantity_change, quantity_before, quantity_after, notes)
            VALUES (?,?,?,?,?,?,?)""",
            (pid, session['user_id'], move_type if move_type in ('stock_in','stock_out','adjustment','sale','return') else 'adjustment',
             -qty, old_stock, new_stock, notes))
        conn.execute("""INSERT INTO product_movements
            (product_id, user_id, move_type, quantity, quantity_before, quantity_after, reference_type, notes)
            VALUES (?,?,?,?,?,?,'manual',?)""",
            (pid, session['user_id'], move_type, -qty, old_stock, new_stock, notes))
        conn.execute("INSERT INTO audit_logs (user_id, action, module, description) VALUES (?,?,?,?)",
            (session['user_id'], move_type.upper(), 'Inventory',
             f"{move_type.title()}: {product['product_name']} -{qty} units. {notes}"))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'new_stock': new_stock})
    except Exception as e:
        conn.rollback(); conn.close()
        return jsonify({'success': False, 'message': str(e)}), 500

@po_bp.route('/purchase-orders/<int:po_id>/reject', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def reject(po_id):
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
    reason = request.form.get('reason', 'Rejected by manager')
    if po and po['status'] not in ('approved', 'cancelled'):
        conn.execute("""UPDATE purchase_orders SET status='cancelled',
                        notes=CASE WHEN notes IS NULL OR notes='' THEN ? ELSE notes||' | REJECTED: '||? END,
                        updated_at=CURRENT_TIMESTAMP WHERE po_id=?""",
                     (f'REJECTED: {reason}', reason, po_id))
        conn.commit()
        audit(session['user_id'], 'PO_REJECTED', f"PO {po['po_number']} rejected: {reason}")
        flash(f'Purchase order {po["po_number"]} has been rejected.', 'warning')
    conn.close()
    return redirect(url_for('purchase_orders.index'))

@po_bp.route('/purchase-orders/<int:po_id>/direct-approve', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def direct_approve(po_id):
    """Approve directly without needing mark-arrived step first."""
    conn = get_db()
    po = conn.execute("SELECT * FROM purchase_orders WHERE po_id=?", (po_id,)).fetchone()
    if not po or po['status'] in ('approved', 'cancelled'):
        flash('Cannot approve this order.', 'danger')
        conn.close()
        return redirect(url_for('purchase_orders.index'))

    # Set all items as fully received if not already
    items = conn.execute("""SELECT poi.*, p.stock as current_stock
                            FROM purchase_order_items poi
                            JOIN products p ON poi.product_id=p.product_id
                            WHERE poi.po_id=?""", (po_id,)).fetchall()
    try:
        arrived_date = request.form.get('arrived_date') or __import__('datetime').date.today().isoformat()
        for item in items:
            recv = item['quantity_received'] if item['quantity_received'] > 0 else item['quantity_ordered']
            # Update received qty if not set
            if item['quantity_received'] == 0:
                conn.execute("UPDATE purchase_order_items SET quantity_received=? WHERE poi_id=?",
                             (recv, item['poi_id']))
            pid = item['product_id']
            old_stock = item['current_stock']
            new_stock = old_stock + recv

            conn.execute("UPDATE products SET stock=?, updated_at=CURRENT_TIMESTAMP WHERE product_id=?",
                         (new_stock, pid))
            batch_num = item['batch_number'] or f"PO-{po['po_number']}-{pid}"
            conn.execute("""INSERT INTO product_batches
                (product_id, batch_number, expiration_date, quantity, remaining_quantity, supplier_id, purchase_price, notes)
                VALUES (?,?,?,?,?,?,?,?)""",
                (pid, batch_num, item['expiration_date'], recv, recv,
                 po['supplier_id'], item['unit_cost'], f"From PO {po['po_number']}"))
            batch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            conn.execute("""INSERT INTO inventory_logs
                (product_id, user_id, action, quantity_change, quantity_before, quantity_after, reference_id, notes)
                VALUES (?,?,'stock_in',?,?,?,?,?)""",
                (pid, session['user_id'], recv, old_stock, new_stock,
                 po['po_number'], f"Direct approved from PO {po['po_number']}"))
            conn.execute("""INSERT INTO product_movements
                (product_id, user_id, move_type, quantity, quantity_before, quantity_after,
                 unit_cost, reference_type, reference_id, batch_id, notes)
                VALUES (?,?,'stock_in',?,?,?,?,'purchase_order',?,?,?)""",
                (pid, session['user_id'], recv, old_stock, new_stock,
                 item['unit_cost'], po['po_number'], batch_id,
                 f"Direct approved via PO {po['po_number']}"))

        conn.execute("""UPDATE purchase_orders SET status='approved', approved_by=?,
                        arrived_date=?, updated_at=CURRENT_TIMESTAMP WHERE po_id=?""",
                     (session['user_id'], arrived_date, po_id))
        conn.execute("INSERT INTO audit_logs (user_id, action, module, description) VALUES (?,?,?,?)",
                     (session['user_id'], 'PO_DIRECT_APPROVE', 'Purchase Orders',
                      f"Direct approved PO {po['po_number']} — stock updated"))
        conn.commit()
        flash(f'✅ PO {po["po_number"]} approved and inventory updated!', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'Error: {str(e)}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('purchase_orders.index'))


# ── AUTO-REORDER: Suggest low-stock items ──────────────────────────────────────
@po_bp.route('/api/purchase-orders/auto-reorder-suggestions')
@login_required
def auto_reorder_suggestions():
    conn = get_db()
    low_items = conn.execute("""
        SELECT p.product_id, p.product_name, p.category, p.unit,
               p.stock, p.low_stock_threshold, p.cost_price, p.supplier_id,
               s.name as supplier_name
        FROM products p
        LEFT JOIN suppliers s ON p.supplier_id = s.supplier_id
        WHERE p.is_active=1 AND p.stock <= p.low_stock_threshold
        ORDER BY (p.low_stock_threshold - p.stock) DESC
    """).fetchall()

    # Calculate suggested reorder qty: enough to bring to 3x the threshold
    suggestions = []
    for item in low_items:
        target = item['low_stock_threshold'] * 3
        suggested_qty = max(target - item['stock'], item['low_stock_threshold'])
        suggestions.append({
            'product_id':        item['product_id'],
            'product_name':      item['product_name'],
            'category':          item['category'],
            'unit':              item['unit'],
            'current_stock':     item['stock'],
            'threshold':         item['low_stock_threshold'],
            'suggested_qty':     suggested_qty,
            'unit_cost':         item['cost_price'],
            'subtotal':          round(suggested_qty * item['cost_price'], 2),
            'supplier_id':       item['supplier_id'],
            'supplier_name':     item['supplier_name'] or 'Unknown',
        })
    conn.close()
    return jsonify({'suggestions': suggestions, 'count': len(suggestions)})


# ── AUTO-REORDER: Create draft PO from suggestions ────────────────────────────
@po_bp.route('/purchase-orders/auto-create', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def auto_create_po():
    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'success': False, 'message': 'No items provided'}), 400

    items_by_supplier = {}
    for item in data['items']:
        sid = item.get('supplier_id') or 0
        items_by_supplier.setdefault(sid, []).append(item)

    conn = get_db()
    created_pos = []
    try:
        for supplier_id, items in items_by_supplier.items():
            if not supplier_id:
                continue  # Skip items with no supplier
            total = sum(i['quantity'] * i['unit_cost'] for i in items)
            po_number = gen_po_number()
            conn.execute("""
                INSERT INTO purchase_orders
                    (po_number, supplier_id, status, total_amount, notes, created_by, created_at, updated_at)
                VALUES (?,?,'pending',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
            """, (po_number, supplier_id, round(total, 2),
                  'Auto-generated reorder — please review before confirming.',
                  session['user_id']))
            po_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            for item in items:
                # Look up product name from DB
                prod = conn.execute("SELECT product_name FROM products WHERE product_id=?",
                                    (item['product_id'],)).fetchone()
                prod_name = prod['product_name'] if prod else 'Unknown Product'
                conn.execute("""
                    INSERT INTO purchase_order_items
                        (po_id, product_id, product_name, quantity_ordered, unit_cost)
                    VALUES (?,?,?,?,?)
                """, (po_id, item['product_id'], prod_name, item['quantity'], item['unit_cost']))

            conn.execute("""
                INSERT INTO audit_logs (user_id, action, module, description) VALUES (?,?,?,?)
            """, (session['user_id'], 'AUTO_PO_CREATED', 'Purchase Orders',
                  f'Auto-reorder PO {po_number} created with {len(items)} item(s) for supplier #{supplier_id}'))
            created_pos.append(po_number)

        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        return jsonify({'success': False, 'message': str(e)}), 500
    conn.close()
    return jsonify({'success': True, 'created': created_pos,
                    'message': f'{len(created_pos)} Purchase Order(s) created as draft. Please review and confirm.'})
