from flask import Blueprint, render_template, request, session, redirect, url_for, flash, send_file, jsonify
from database.db import get_db, DB_PATH
from functools import wraps
import os, shutil, datetime

backup_bp = Blueprint('backup', __name__)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') != 'admin':
            flash('Only admins can access backup & restore.', 'danger')
            return redirect(url_for('dashboard.index'))
        return f(*args, **kwargs)
    return decorated

BACKUP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'database', 'backups')

@backup_bp.route('/backup')
@login_required
@admin_required
def index():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_files = []
    for fname in sorted(os.listdir(BACKUP_DIR), reverse=True):
        if fname.endswith('.db'):
            fpath = os.path.join(BACKUP_DIR, fname)
            stat = os.stat(fpath)
            backup_files.append({
                'name': fname,
                'size_kb': round(stat.st_size / 1024, 1),
                'created': datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
            })
    return render_template('backup.html', backup_files=backup_files)

@backup_bp.route('/backup/create', methods=['POST'])
@login_required
@admin_required
def create_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_name = f'vsms_backup_{timestamp}.db'
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    # Use SQLite online backup via Python
    import sqlite3
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()

    # Log audit
    try:
        conn = get_db()
        conn.execute("INSERT INTO audit_logs (user_id, action, module, description) VALUES (?,?,?,?)",
                     (session['user_id'], 'BACKUP_CREATED', 'Backup', f'Backup created: {backup_name}'))
        conn.commit(); conn.close()
    except: pass

    flash(f'✅ Backup created successfully: {backup_name}', 'success')
    return redirect(url_for('backup.index'))

@backup_bp.route('/backup/download/<filename>')
@login_required
@admin_required
def download_backup(filename):
    # Sanitize filename to prevent path traversal
    filename = os.path.basename(filename)
    if not filename.endswith('.db'):
        flash('Invalid backup file.', 'danger')
        return redirect(url_for('backup.index'))

    backup_path = os.path.join(BACKUP_DIR, filename)
    if not os.path.exists(backup_path):
        flash('Backup file not found.', 'danger')
        return redirect(url_for('backup.index'))

    return send_file(backup_path, as_attachment=True, download_name=filename)

@backup_bp.route('/backup/restore', methods=['POST'])
@login_required
@admin_required
def restore_backup():
    source = request.form.get('source', 'file')  # 'file' or 'existing'

    if source == 'existing':
        filename = os.path.basename(request.form.get('filename', ''))
        if not filename.endswith('.db'):
            flash('Invalid backup file selected.', 'danger')
            return redirect(url_for('backup.index'))
        restore_path = os.path.join(BACKUP_DIR, filename)
        if not os.path.exists(restore_path):
            flash('Backup file not found.', 'danger')
            return redirect(url_for('backup.index'))
    else:
        # Upload file restore
        if 'backup_file' not in request.files:
            flash('No file uploaded.', 'danger')
            return redirect(url_for('backup.index'))
        f = request.files['backup_file']
        if not f.filename.endswith('.db'):
            flash('Please upload a valid .db backup file.', 'danger')
            return redirect(url_for('backup.index'))
        os.makedirs(BACKUP_DIR, exist_ok=True)
        restore_path = os.path.join(BACKUP_DIR, '_upload_restore_temp.db')
        f.save(restore_path)

    # Safety: create an auto-backup before restoring
    timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    pre_restore_backup = os.path.join(BACKUP_DIR, f'pre_restore_{timestamp}.db')
    try:
        import sqlite3
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(pre_restore_backup)
        src.backup(dst)
        dst.close(); src.close()
    except Exception as e:
        flash(f'Could not create safety backup before restore: {e}', 'danger')
        return redirect(url_for('backup.index'))

    # Validate that the uploaded file is a valid SQLite database
    try:
        import sqlite3
        test = sqlite3.connect(restore_path)
        test.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        test.close()
    except Exception:
        flash('The uploaded file is not a valid SQLite database.', 'danger')
        return redirect(url_for('backup.index'))

    # Restore: copy backup over current DB
    try:
        import sqlite3
        src = sqlite3.connect(restore_path)
        dst = sqlite3.connect(DB_PATH)
        src.backup(dst)
        dst.close(); src.close()
    except Exception as e:
        flash(f'Restore failed: {e}. Your original database was preserved as {os.path.basename(pre_restore_backup)}.', 'danger')
        return redirect(url_for('backup.index'))

    flash(f'✅ Database restored successfully! A safety backup was saved as {os.path.basename(pre_restore_backup)}.', 'success')
    return redirect(url_for('backup.index'))

@backup_bp.route('/backup/delete/<filename>', methods=['POST'])
@login_required
@admin_required
def delete_backup(filename):
    filename = os.path.basename(filename)
    if not filename.endswith('.db'):
        flash('Invalid file.', 'danger')
        return redirect(url_for('backup.index'))
    backup_path = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(backup_path):
        os.remove(backup_path)
        flash(f'Backup {filename} deleted.', 'success')
    return redirect(url_for('backup.index'))
