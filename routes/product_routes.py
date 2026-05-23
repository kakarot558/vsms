from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify, Response
from database.db import get_db
from werkzeug.utils import secure_filename
from functools import wraps
from datetime import datetime
import os, random, string, csv, io

product_bp = Blueprint('products', __name__)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

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
                flash('Access denied. Insufficient permissions.', 'danger')
                return redirect(url_for('dashboard.index'))
            return f(*args, **kwargs)
        return decorated
    return decorator

def allowed_file(fn):
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def log_audit(user_id, action, module, description):
    try:
        conn = get_db()
        conn.execute("INSERT INTO audit_logs (user_id,action,module,description) VALUES (?,?,?,?)",
                     (user_id, action, module, description))
        conn.commit(); conn.close()
    except: pass

def generate_barcode():
    return '890' + ''.join([str(random.randint(0,9)) for _ in range(10)])

@product_bp.route('/products')
@login_required
def index():
    conn = get_db()
    search       = request.args.get('search', '')
    category     = request.args.get('category', '')
    stock_filter = request.args.get('stock_filter', '')
    page         = int(request.args.get('page', 1))
    per_page     = 15
    offset       = (page - 1) * per_page

    query = """SELECT p.*, s.name as supplier_name
               FROM products p LEFT JOIN suppliers s ON p.supplier_id=s.supplier_id
               WHERE p.is_active=1"""
    params = []
    if search:
        query += " AND (p.product_name LIKE ? OR p.barcode LIKE ? OR p.brand LIKE ?)"
        params += [f'%{search}%', f'%{search}%', f'%{search}%']
    if category:
        query += " AND p.category=?"
        params.append(category)
    if stock_filter == 'low':
        query += " AND p.stock > 0 AND p.stock <= p.low_stock_threshold"
    elif stock_filter == 'out':
        query += " AND p.stock <= 0"
    elif stock_filter == 'ok':
        query += " AND p.stock > p.low_stock_threshold"

    total      = conn.execute(query.replace("SELECT p.*, s.name as supplier_name","SELECT COUNT(*)"), params).fetchone()[0]
    products   = conn.execute(query + f" ORDER BY p.product_name ASC LIMIT {per_page} OFFSET {offset}", params).fetchall()
    categories = conn.execute("SELECT DISTINCT category FROM products WHERE is_active=1 ORDER BY category").fetchall()
    suppliers  = conn.execute("SELECT * FROM suppliers WHERE is_active=1 ORDER BY name").fetchall()

    total_pages      = (total + per_page - 1) // per_page
    all_prods_q      = conn.execute("SELECT product_id, product_name, barcode, price, cost_price FROM products WHERE is_active=1 ORDER BY product_name").fetchall()
    low_stock_count  = conn.execute("SELECT COUNT(*) FROM products WHERE stock<=low_stock_threshold AND stock>0 AND is_active=1").fetchone()[0]
    inv_val          = conn.execute("SELECT COALESCE(SUM(stock*cost_price),0) FROM products WHERE is_active=1").fetchone()[0]
    ret_val          = conn.execute("SELECT COALESCE(SUM(stock*price),0) FROM products WHERE is_active=1").fetchone()[0]
    conn.close()
    return render_template('products.html',
        products=products, categories=categories, suppliers=suppliers,
        search=search, category=category, stock_filter=stock_filter,
        page=page, total_pages=total_pages, total=total,
        all_products=[dict(p) for p in all_prods_q],
        low_stock_count=low_stock_count,
        inventory_value=inv_val,
        retail_value=ret_val)

@product_bp.route('/products/add', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def add():
    data = request.form
    image_filename = None
    if 'image' in request.files:
        file = request.files['image']
        if file and file.filename and allowed_file(file.filename):
            from flask import current_app
            filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
            file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
            image_filename = filename

    barcode = data.get('barcode') or generate_barcode()
    conn = get_db()
    try:
        conn.execute("""INSERT INTO products
            (product_name,barcode,category,brand,description,price,cost_price,
             stock,unit,low_stock_threshold,image,supplier_id,requires_prescription)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (data['product_name'], barcode, data['category'], data.get('brand',''),
             data.get('description',''), float(data['price']), float(data.get('cost_price',0)),
             int(data.get('stock',0)), data.get('unit','pcs'),
             int(data.get('low_stock_threshold',10)), image_filename,
             data.get('supplier_id') or None, int(data.get('requires_prescription',0))))
        product_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        initial_stock = int(data.get('stock', 0))
        if initial_stock > 0:
            batch_num = f"BATCH-{data['product_name'][:3].upper()}-{datetime.now().strftime('%Y%m%d')}"
            conn.execute("""INSERT INTO product_batches
                (product_id,batch_number,expiration_date,quantity,remaining_quantity,supplier_id)
                VALUES (?,?,?,?,?,?)""",
                (product_id, batch_num, data.get('expiration_date') or None,
                 initial_stock, initial_stock, data.get('supplier_id') or None))
            conn.execute("""INSERT INTO inventory_logs
                (product_id,user_id,action,quantity_change,quantity_before,quantity_after,notes)
                VALUES (?,?,'stock_in',?,0,?,?)""",
                (product_id, session['user_id'], initial_stock, initial_stock, 'Initial stock'))
        conn.commit()
        log_audit(session['user_id'], 'CREATE', 'Products', f"Added product: {data['product_name']}")
        flash('Product added successfully!', 'success')
    except Exception as e:
        flash(f'Error adding product: {str(e)}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('products.index'))

@product_bp.route('/products/<int:pid>/edit', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def edit(pid):
    data = request.form
    conn = get_db()
    old = conn.execute("SELECT * FROM products WHERE product_id=?", (pid,)).fetchone()
    image_filename = old['image']
    if 'image' in request.files:
        file = request.files['image']
        if file and file.filename and allowed_file(file.filename):
            from flask import current_app
            filename = secure_filename(f"{datetime.now().timestamp()}_{file.filename}")
            file.save(os.path.join(current_app.config['UPLOAD_FOLDER'], filename))
            image_filename = filename
    try:
        conn.execute("""UPDATE products SET
            product_name=?,barcode=?,category=?,brand=?,description=?,
            price=?,cost_price=?,unit=?,low_stock_threshold=?,image=?,
            supplier_id=?,requires_prescription=?,updated_at=CURRENT_TIMESTAMP
            WHERE product_id=?""",
            (data['product_name'], data.get('barcode', old['barcode']),
             data['category'], data.get('brand',''), data.get('description',''),
             float(data['price']), float(data.get('cost_price',0)),
             data.get('unit','pcs'), int(data.get('low_stock_threshold',10)),
             image_filename, data.get('supplier_id') or None,
             int(data.get('requires_prescription',0)), pid))
        conn.commit()
        log_audit(session['user_id'], 'UPDATE', 'Products', f"Updated product ID {pid}: {data['product_name']}")
        flash('Product updated successfully!', 'success')
    except Exception as e:
        flash(f'Error: {str(e)}', 'danger')
    finally:
        conn.close()
    return redirect(url_for('products.index'))

@product_bp.route('/products/<int:pid>/delete', methods=['POST'])
@login_required
@role_required('admin')
def delete(pid):
    conn = get_db()
    p = conn.execute("SELECT product_name FROM products WHERE product_id=?", (pid,)).fetchone()
    conn.execute("UPDATE products SET is_active=0 WHERE product_id=?", (pid,))
    conn.commit(); conn.close()
    log_audit(session['user_id'], 'DELETE', 'Products', f"Deleted product: {p['product_name']}")
    flash('Product deleted successfully!', 'success')
    return redirect(url_for('products.index'))

@product_bp.route('/api/products/barcode/<barcode>')
@login_required
def get_by_barcode(barcode):
    conn = get_db()
    product = conn.execute("""SELECT p.*, s.name as supplier_name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id=s.supplier_id
        WHERE p.barcode=? AND p.is_active=1""", (barcode,)).fetchone()
    conn.close()
    if product:
        return jsonify({'product': dict(product)})
    return jsonify({'product': None, 'message': 'Product not found'}), 404

@product_bp.route('/scan/<barcode>')
@login_required
def scan_barcode_page(barcode):
    """Mobile-friendly page: scan a barcode URL → auto-push to POS cart → show result."""
    import json as _json
    conn = get_db()
    product = conn.execute("""SELECT p.*, s.name as supplier_name
        FROM products p LEFT JOIN suppliers s ON p.supplier_id=s.supplier_id
        WHERE p.barcode=? AND p.is_active=1""", (barcode,)).fetchone()
    conn.close()
    product_json = _json.dumps(dict(product)) if product else 'null'
    return render_template('scan_result.html', product=product, barcode=barcode, product_json=product_json)

@product_bp.route('/api/products/search')
@login_required
def search_api():
    q     = request.args.get('q', '')
    limit = min(int(request.args.get('limit', 60)), 200)
    cat   = request.args.get('category', '')
    conn  = get_db()
    base  = """SELECT product_id, product_name, barcode, price, stock, unit, category
               FROM products WHERE is_active=1 AND stock > 0"""
    params = []
    if q:
        base += " AND (product_name LIKE ? OR barcode LIKE ? OR brand LIKE ?)"
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if cat:
        base += " AND category=?"
        params.append(cat)
    base += f" ORDER BY product_name LIMIT {limit}"
    products = conn.execute(base, params).fetchall()
    conn.close()
    return jsonify({'products': [dict(p) for p in products]})

@product_bp.route('/api/products/generate-barcode')
@login_required
def gen_barcode():
    return jsonify({'barcode': generate_barcode()})

@product_bp.route('/products/<int:pid>/batches')
@login_required
def batches(pid):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE product_id=?", (pid,)).fetchone()
    batches = conn.execute("""SELECT pb.*, s.name as supplier_name
        FROM product_batches pb LEFT JOIN suppliers s ON pb.supplier_id=s.supplier_id
        WHERE pb.product_id=? ORDER BY pb.expiration_date ASC""", (pid,)).fetchall()
    conn.close()
    return jsonify({'product': dict(product), 'batches': [dict(b) for b in batches]})


# ── EXPORT ──────────────────────────────────────────────────────────────────

EXPORT_COLUMNS = [
    'product_name', 'barcode', 'category', 'brand', 'description',
    'price', 'cost_price', 'stock', 'unit', 'low_stock_threshold',
    'requires_prescription'
]

@product_bp.route('/products/export')
@login_required
def export_csv():
    conn = get_db()
    products = conn.execute(
        "SELECT " + ", ".join(EXPORT_COLUMNS) +
        " FROM products WHERE is_active=1 ORDER BY product_name"
    ).fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS)
    for p in products:
        writer.writerow([p[c] for c in EXPORT_COLUMNS])

    filename = f"products_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    log_audit(session['user_id'], 'EXPORT', 'Products', f"Exported {len(products)} products to CSV")
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


# ── CSV TEMPLATE ─────────────────────────────────────────────────────────────

@product_bp.route('/products/import/template')
@login_required
def download_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS)
    # One example row
    writer.writerow([
        'Sample Product', '', 'Veterinary Medicine', 'BrandX',
        'Sample description', '99.99', '60.00', '50', 'pcs', '10', '0'
    ])
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename="products_import_template.csv"'}
    )


# ── IMPORT ───────────────────────────────────────────────────────────────────

@product_bp.route('/products/import', methods=['POST'])
@login_required
@role_required('admin', 'inventory_manager')
def import_csv():
    if 'csv_file' not in request.files:
        flash('No file uploaded.', 'danger')
        return redirect(url_for('products.index'))

    file = request.files['csv_file']
    if not file or not file.filename.endswith('.csv'):
        flash('Please upload a valid CSV file.', 'danger')
        return redirect(url_for('products.index'))

    stream = io.StringIO(file.stream.read().decode('utf-8-sig'))
    reader = csv.DictReader(stream)

    required_cols = {'product_name', 'price'}
    if not required_cols.issubset(set(reader.fieldnames or [])):
        missing = required_cols - set(reader.fieldnames or [])
        flash(f'CSV is missing required column(s): {", ".join(missing)}', 'danger')
        return redirect(url_for('products.index'))

    conn = get_db()
    created = updated = skipped = 0
    errors = []

    for i, row in enumerate(reader, start=2):  # row 1 = header
        product_name = (row.get('product_name') or '').strip()
        price_raw    = (row.get('price') or '').strip()

        if not product_name:
            errors.append(f"Row {i}: product_name is empty — skipped.")
            skipped += 1
            continue
        try:
            price = float(price_raw)
        except ValueError:
            errors.append(f"Row {i}: invalid price '{price_raw}' — skipped.")
            skipped += 1
            continue

        barcode           = (row.get('barcode') or '').strip() or generate_barcode()
        category          = (row.get('category') or 'Uncategorized').strip()
        brand             = (row.get('brand') or '').strip()
        description       = (row.get('description') or '').strip()
        unit              = (row.get('unit') or 'pcs').strip()

        try:
            cost_price        = float(row.get('cost_price') or 0)
            stock             = int(float(row.get('stock') or 0))
            low_stock_threshold = int(float(row.get('low_stock_threshold') or 10))
            requires_rx       = int(float(row.get('requires_prescription') or 0))
        except (ValueError, TypeError):
            errors.append(f"Row {i}: numeric conversion error — skipped.")
            skipped += 1
            continue

        try:
            existing = conn.execute(
                "SELECT product_id, stock FROM products WHERE barcode=? AND is_active=1",
                (barcode,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE products SET
                        product_name=?, category=?, brand=?, description=?,
                        price=?, cost_price=?, unit=?, low_stock_threshold=?,
                        requires_prescription=?, updated_at=CURRENT_TIMESTAMP
                    WHERE product_id=?
                """, (product_name, category, brand, description,
                      price, cost_price, unit, low_stock_threshold,
                      requires_rx, existing['product_id']))
                updated += 1
            else:
                conn.execute("""
                    INSERT INTO products
                        (product_name, barcode, category, brand, description,
                         price, cost_price, stock, unit, low_stock_threshold,
                         requires_prescription)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (product_name, barcode, category, brand, description,
                      price, cost_price, stock, unit, low_stock_threshold,
                      requires_rx))
                pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                if stock > 0:
                    batch_num = f"IMPORT-{product_name[:3].upper()}-{datetime.now().strftime('%Y%m%d')}"
                    conn.execute("""
                        INSERT INTO product_batches
                            (product_id, batch_number, quantity, remaining_quantity)
                        VALUES (?,?,?,?)
                    """, (pid, batch_num, stock, stock))
                    conn.execute("""
                        INSERT INTO inventory_logs
                            (product_id, user_id, action, quantity_change,
                             quantity_before, quantity_after, notes)
                        VALUES (?,?,'stock_in',?,0,?,'CSV Import')
                    """, (pid, session['user_id'], stock, stock))
                created += 1
        except Exception as e:
            errors.append(f"Row {i}: {str(e)} — skipped.")
            skipped += 1
            continue

    conn.commit()
    conn.close()

    summary = f"Import complete: {created} created, {updated} updated, {skipped} skipped."
    log_audit(session['user_id'], 'IMPORT', 'Products', summary)

    if errors:
        flash(summary + " Errors: " + " | ".join(errors[:5]) +
              (f" … and {len(errors)-5} more." if len(errors) > 5 else ""), 'warning')
    else:
        flash(summary, 'success')

    return redirect(url_for('products.index'))
