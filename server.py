import base64
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import csv
import io
import re
import json
import os
from pathlib import Path
import sqlite3
import struct
import uuid

from PIL import Image, ImageDraw, ImageFont, ImageOps
from receipt_text import render_text, validate_fonts

ROOT = Path(__file__).resolve().parent
DB = Path(os.getenv('DB_PATH', '/data/confessions.sqlite3'))
TOKEN = os.environ['PRINT_TOKEN']
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
if len(TOKEN) < 32:
    raise ValueError('PRINT_TOKEN must be at least 32 characters')
FONT_DIR = Path('/usr/share/fonts/truetype/liberation')
if not FONT_DIR.exists():
    FONT_DIR = Path('/usr/share/fonts/truetype/liberation2')
HEAD = Image.open(ROOT / 'assets/bon-head.png').convert('L')
FOOT = Image.open(ROOT / 'assets/bon-foot.png').convert('L')


def render(number, text):
    heading = ImageFont.truetype(str(FONT_DIR / 'LiberationSans-Bold.ttf'), 38)
    block = render_text(text)
    body_y = 785 - block.baseline
    footer_y = max(853, body_y + block.image.height + 48)
    paper = Image.new('L', (945, footer_y + FOOT.height), 255)
    paper.paste(HEAD, (0, 0))
    paper.paste(FOOT, (0, footer_y))
    paper.paste(block.image, (79 - block.origin_x, body_y))
    draw = ImageDraw.Draw(paper)
    draw.rectangle((197, 425, 748, 477), fill=255)
    label = f'CONFESSION NO. {number:03d}'
    advances = [heading.getlength(c) + 4.2 for c in label]
    x = (945 - sum(advances) + 4.2) / 2
    for character, advance in zip(label, advances):
        draw.text((x, 464), character, font=heading, fill=0, anchor='ls')
        x += advance
    paper = paper.crop((48, 0, 897, paper.height))
    paper = paper.resize((576, round(paper.height * 576 / paper.width)), Image.Resampling.LANCZOS)
    raster = ImageOps.invert(paper).point(lambda p: 255 if p >= 128 else 0, '1')
    return b'\x1b@\x1dv0\x00' + struct.pack('<HH', 72, raster.height) + raster.tobytes() + b'\x1dVB\x00'


def render_promo():
    """One advertising receipt, no confession number; QR stays crisp at 576px."""
    head = Image.new('L', (945, HEAD.height - 64), 255)
    head.paste(HEAD.crop((0, 0, 945, 416)), (0, 0))
    head.paste(HEAD.crop((0, 480, 945, HEAD.height)), (0, 416))
    head = head.crop((48, 0, 897, head.height))
    head = head.resize((576, round(head.height * 576 / 849)), Image.Resampling.LANCZOS)
    # Keep the original star divider and website, without the absolution text.
    foot = Image.new('L', (849, 85 + FOOT.height - 210), 255)
    foot.paste(FOOT.crop((48, 0, 897, 65)), (0, 0))
    foot.paste(FOOT.crop((48, 210, 897, FOOT.height)), (0, 85))
    foot = foot.resize((576, round(foot.height * 576 / 849)), Image.Resampling.LANCZOS)
    body_height = 560
    paper = Image.new('L', (576, head.height + body_height + foot.height), 255)
    paper.paste(head, (0, 0))
    paper.paste(foot, (0, head.height + body_height))
    draw = ImageDraw.Draw(paper)
    font = ImageFont.truetype(str(FONT_DIR / 'LiberationSans-Bold.ttf'), 26)
    title = 'MAKE YOUR CONFESSION'
    advances = [font.getlength(c) + 2.85 for c in title]
    x = (576 - sum(advances) + 2.85) / 2
    for char, advance in zip(title, advances):
        draw.text((x, head.height + 45), char, font=font, fill=0, anchor='ls')
        x += advance
    with Image.open(ROOT / 'assets/promo-qr.png') as source:
        qr = source.convert('L').resize((444, 444), Image.Resampling.NEAREST)
    paper.paste(qr, (66, head.height + 78))
    raster = ImageOps.invert(paper).point(lambda p: 255 if p >= 128 else 0, '1')
    return b'\x1b@\x1dv0\x00' + struct.pack('<HH', 72, raster.height) + raster.tobytes() + b'\x1dVB\x00'


def connect():
    return sqlite3.connect(DB, timeout=10, isolation_level='IMMEDIATE')


def initialize():
    validate_fonts()
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, payload BLOB, confession TEXT)')
        columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
        for column, definition in {'confession': 'TEXT', 'created_at': 'TEXT', 'reprints': 'INTEGER NOT NULL DEFAULT 0', 'revision': 'INTEGER NOT NULL DEFAULT 0', 'claim_token': 'TEXT', 'copies': 'INTEGER NOT NULL DEFAULT 1', 'print_copies': 'INTEGER NOT NULL DEFAULT 1'}.items():
            if column not in columns:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {column} {definition}')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO settings VALUES ('paused','0')")
        # Existing jobs retain one copy. New submissions snapshot this setting.
        db.execute("INSERT OR IGNORE INTO settings VALUES ('receipt_copies','2')")
        db.execute("INSERT OR IGNORE INTO settings VALUES ('run_id',?)", (uuid.uuid4().hex,))
        db.execute('''CREATE TABLE IF NOT EXISTS archived_jobs (
            run_id TEXT NOT NULL, archived_at TEXT NOT NULL, number INTEGER NOT NULL,
            request_id TEXT NOT NULL, state TEXT NOT NULL, confession TEXT, payload BLOB,
            created_at TEXT, reprints INTEGER NOT NULL, PRIMARY KEY(run_id, number))''')
        archived_columns = {row[1] for row in db.execute('PRAGMA table_info(archived_jobs)')}
        for column in ('copies', 'print_copies'):
            if column not in archived_columns:
                db.execute(f'ALTER TABLE archived_jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1')
        db.execute('CREATE INDEX IF NOT EXISTS archived_request ON archived_jobs(request_id)')
        db.execute('CREATE INDEX IF NOT EXISTS print_attempt ON jobs(claim_token)')
        db.execute('''CREATE TABLE IF NOT EXISTS promo_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL,
            state TEXT NOT NULL, payload BLOB NOT NULL, claim_token TEXT,
            created_at TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS promo_attempt ON promo_jobs(claim_token)')
        # Never silently resend an unfinished job from the older client.
        db.execute("UPDATE jobs SET state='uncertain' WHERE state='claimed' AND claim_token IS NULL")


def setting(db, key):
    return db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]


def csrf_token(db):
    return hmac.new(ADMIN_PASSWORD.encode(), ('admin:' + setting(db, 'run_id')).encode(), 'sha256').hexdigest()


def preview_bytes(payload):
    """Decode exactly the 576px raster sent to the Epson, including legacy jobs."""
    if not payload or not payload.startswith(b'\x1b@\x1dv0\x00') or len(payload) < 10:
        raise ValueError('Unsupported receipt raster')
    width_bytes, height = struct.unpack('<HH', payload[6:10])
    if width_bytes != 72 or not 1 <= height <= 40000:
        raise ValueError('Invalid receipt size')
    raster = payload[10:10 + width_bytes * height]
    if len(raster) != width_bytes * height:
        raise ValueError('Incomplete receipt raster')
    image = Image.frombytes('1', (width_bytes * 8, height), raster)
    image = ImageOps.invert(image.convert('L'))
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


def safe_csv_cell(value):
    text = '' if value is None else str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        return "'" + text
    return text


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data, mime='application/json', headers=None):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(code)
        for name, value in {'Content-Type': mime, 'Content-Length': str(len(data)), 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer', **(headers or {})}.items():
            self.send_header(name, value)
        if self.path.startswith('/admin'):
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def admin_auth(self):
        if len(ADMIN_PASSWORD) < 32:
            self.reply(404, {})
            return False
        expected = b'Basic ' + base64.b64encode(f'admin:{ADMIN_PASSWORD}'.encode())
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected):
            self.reply(401, {'error': 'Admin-Anmeldung erforderlich.'}, headers={'WWW-Authenticate': 'Basic realm="Healthy Confessions Admin", charset="UTF-8"'})
            return False
        return True

    def do_GET(self):
        if self.path == '/admin' or self.path.startswith('/admin/'):
            if self.admin_auth():
                return self.admin_get()
            return
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/logo.png': ('assets/logo.png', 'image/png'), '/forgiven.gif': ('assets/forgiven.gif', 'image/gif'), '/pickup-can.png': ('assets/pickup-can.png', 'image/png'), '/pickup-katakana.png': ('assets/pickup-katakana.png', 'image/png')}
        if self.path not in files:
            return self.reply(404, {})
        name, mime = files[self.path]
        asset = ROOT / name
        if self.path == '/forgiven.gif' and not asset.is_file():
            asset, mime = ROOT / 'assets/forgiven.png', 'image/png'
        if not asset.is_file():
            return self.reply(404, {})
        self.reply(200, asset.read_bytes(), mime)

    def admin_get(self):
        with closing(connect()) as db, db:
            db.execute('BEGIN')
            if self.path in ('/admin', '/admin/'):
                page = (ROOT / 'admin.html').read_text().replace('__CSRF_TOKEN__', csrf_token(db))
                return self.reply(200, page.encode(), 'text/html; charset=utf-8')
            if self.path == '/admin/admin.js':
                return self.reply(200, (ROOT / 'admin.js').read_bytes(), 'text/javascript; charset=utf-8')
            if self.path == '/admin/promo.png':
                return self.reply(200, preview_bytes(render_promo()), 'image/png')
            if self.path == '/admin/api/promo-state':
                rows = db.execute('SELECT id,state,created_at FROM promo_jobs ORDER BY id DESC LIMIT 10').fetchall()
                return self.reply(200, {'jobs': [{'id': r[0], 'state': r[1], 'createdAt': r[2]} for r in rows]})
            if self.path == '/admin/api/state':
                rows = db.execute('SELECT id,confession,state,reprints,revision,created_at,payload IS NOT NULL,copies,print_copies FROM jobs ORDER BY id DESC').fetchall()
                jobs = [{'id': r[0], 'number': f'{r[0]:03d}', 'confession': r[1], 'state': r[2], 'reprints': r[3], 'revision': r[4], 'createdAt': r[5], 'canPreview': r[1] is not None or bool(r[6]), 'canReprint': r[2] in ('sent', 'uncertain') and (r[1] is not None or bool(r[6])), 'copies': r[7], 'printCopies': r[8]} for r in rows]
                sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='jobs'").fetchone()
                return self.reply(200, {'jobs': jobs, 'paused': setting(db, 'paused') == '1', 'receiptCopies': int(setting(db, 'receipt_copies')), 'runId': setting(db, 'run_id'), 'csrf': csrf_token(db), 'nextNumber': f'{(sequence[0] if sequence else 0) + 1:03d}'})
            if self.path in ('/admin/export.csv', '/admin/export-all.csv'):
                run_id = setting(db, 'run_id')
                rows = db.execute('SELECT id,confession,state,created_at,reprints FROM jobs ORDER BY id').fetchall()
                output = io.StringIO(newline='')
                writer = csv.writer(output, delimiter=';')
                writer.writerow(['Durchlauf', 'Bonnummer', 'Confession', 'Druckstatus', 'Erstellt (UTC)', 'Nachdruckauftraege', 'Archiviert (UTC)'])
                for r in rows:
                    writer.writerow([run_id, f'{r[0]:03d}', safe_csv_cell(r[1]), r[2], r[3] or '', r[4], ''])
                if self.path == '/admin/export-all.csv':
                    for r in db.execute('SELECT run_id,number,confession,state,created_at,reprints,archived_at FROM archived_jobs ORDER BY archived_at,number'):
                        writer.writerow([r[0], f'{r[1]:03d}', safe_csv_cell(r[2]), r[3], r[4] or '', r[5], r[6]])
                name = 'healthy-confessions-all.csv' if self.path.endswith('export-all.csv') else 'healthy-confessions.csv'
                return self.reply(200, output.getvalue().encode('utf-8-sig'), 'text/csv; charset=utf-8', {'Content-Disposition': f'attachment; filename="{name}"'})
            match = re.fullmatch(r'/admin/receipt/([0-9]+)\.png(?:\?run=([a-f0-9]{32}))?', self.path)
            if match:
                if match[2] and match[2] != setting(db, 'run_id'):
                    return self.reply(409, {'error': 'Durchlauf wurde zurückgesetzt. Bitte aktualisieren.'})
                job = db.execute('SELECT id,confession,payload FROM jobs WHERE id=?', (int(match[1]),)).fetchone()
                if not job or (job[1] is None and job[2] is None):
                    return self.reply(404, {'error': 'Für diesen alten Eintrag sind keine Druckdaten gespeichert.'})
                try:
                    return self.reply(200, preview_bytes(job[2] if job[2] is not None else render(job[0], job[1])), 'image/png')
                except ValueError:
                    return self.reply(422, {'error': 'Druckdaten sind nicht lesbar.'})
        return self.reply(404, {})

    def do_POST(self):
        is_admin = self.path.startswith('/admin/')
        if is_admin and not self.admin_auth():
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 8192:
                return self.reply(400, {})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                return self.reply(400, {})
        except (ValueError, UnicodeError):
            return self.reply(400, {})
        if is_admin:
            return self.admin_post(body)
        if self.path == '/api/confessions':
            return self.submit(body)
        if self.path not in ('/api/print/claim', '/api/print/finish'):
            return self.reply(404, {})
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + TOKEN).encode()):
            return self.reply(401, {})
        return self.claim(body) if self.path.endswith('/claim') else self.finish_print(body)

    def admin_post(self, body):
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', '').encode(), csrf_token(db).encode()):
                return self.reply(403, {'error': 'Admin-Seite ist veraltet. Bitte neu laden.'})
            if self.path == '/admin/api/promo':
                try:
                    request_id = str(uuid.UUID(str(body.get('requestId'))))
                except ValueError:
                    return self.reply(400, {'error': 'Ungültige Druckanfrage.'})
                existing = db.execute('SELECT id FROM promo_jobs WHERE request_id=?', (request_id,)).fetchone()
                if existing:
                    promo_id = existing[0]
                else:
                    promo_id = db.execute("INSERT INTO promo_jobs(request_id,state,payload,created_at) VALUES (?,'queued',?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (request_id, render_promo())).lastrowid
                message = 'QR-Werbebon zum Druck eingereiht: 1 Exemplar, ohne Confession-Nummer.'
            elif self.path == '/admin/api/pause':
                if type(body.get('paused')) is not bool:
                    return self.reply(400, {'error': 'Ungültiger Druckstatus.'})
                db.execute("UPDATE settings SET value=? WHERE key='paused'", ('1' if body['paused'] else '0',))
                message = 'Druck pausiert. Beichten werden weiter gespeichert.' if body['paused'] else 'Druck freigegeben. Der Druckclient kann wartende Bons abholen.'
            elif self.path == '/admin/api/copies':
                if type(body.get('copies')) is not int or body['copies'] not in (1, 2):
                    return self.reply(400, {'error': 'Bitte 1 oder 2 Bons auswählen.'})
                if type(body.get('expectedCopies')) is not int or body['expectedCopies'] != int(setting(db, 'receipt_copies')):
                    return self.reply(409, {'error': 'Die Einstellung wurde inzwischen geändert. Bitte aktualisieren.'})
                db.execute("UPDATE settings SET value=? WHERE key='receipt_copies'", (str(body['copies']),))
                message = f"Neue Confessions erhalten {body['copies']} Bon(s). Bestehende Aufträge bleiben unverändert."
            elif self.path == '/admin/api/reprint':
                if type(body.get('id')) is not int or type(body.get('revision')) is not int:
                    return self.reply(400, {'error': 'Ungültige Bonnummer.'})
                copies = body.get('copies', 1)
                if type(copies) is not int or copies not in (1, 2):
                    return self.reply(400, {'error': 'Bitte 1 oder 2 Bons auswählen.'})
                job = db.execute('SELECT id,state,confession,payload,revision FROM jobs WHERE id=?', (body['id'],)).fetchone()
                if not job:
                    return self.reply(404, {'error': 'Bon nicht gefunden.'})
                if job[4] != body['revision'] or job[1] not in ('sent', 'uncertain'):
                    return self.reply(409, {'error': 'Druckauftrag wurde bereits geändert oder wartet schon. Bitte aktualisieren.'})
                if job[2] is None and job[3] is None:
                    return self.reply(409, {'error': 'Für diesen alten Bon wurde kein Text gespeichert.'})
                payload = job[3] if job[3] is not None else render(job[0], job[2])
                db.execute("UPDATE jobs SET state='queued',payload=?,print_copies=?,reprints=reprints+1,revision=revision+1,claim_token=NULL WHERE id=?", (payload, copies, job[0]))
                message = f'Bon {job[0]:03d}: {copies} Exemplar(e) zum Nachdruck eingereiht. Die Nummer bleibt gleich.'
            elif self.path == '/admin/api/reset':
                if body.get('confirmation') != 'RESET 001':
                    return self.reply(400, {'error': 'Bitte exakt RESET 001 eingeben.'})
                if setting(db, 'paused') != '1':
                    return self.reply(409, {'error': 'Bitte zuerst den Druck pausieren.'})
                if db.execute("SELECT 1 FROM jobs WHERE state='claimed' LIMIT 1").fetchone():
                    return self.reply(409, {'error': 'Ein Druckauftrag läuft noch. Bitte dessen Abschluss abwarten.'})
                db.execute("INSERT INTO archived_jobs(run_id,archived_at,number,request_id,state,confession,payload,created_at,reprints,copies,print_copies) SELECT ?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),id,request_id,state,confession,payload,created_at,reprints,copies,print_copies FROM jobs", (setting(db, 'run_id'),))
                db.execute('DELETE FROM jobs')
                db.execute("DELETE FROM sqlite_sequence WHERE name='jobs'")
                db.execute("UPDATE settings SET value=? WHERE key='run_id'", (uuid.uuid4().hex,))
                message = 'Durchlauf archiviert. Nächster Bon: 001. Druck bleibt pausiert.'
            else:
                return self.reply(404, {})
            token = csrf_token(db)
        return self.reply(200, {'message': message, 'csrf': token})

    def submit(self, body):
        text = body.get('confession')
        try:
            request_id = str(uuid.UUID(str(body.get('requestId'))))
            if not isinstance(text, str) or not text.strip() or '\x00' in text or len(text.encode('utf-16-le')) > 600:
                return self.reply(400, {})
        except (ValueError, UnicodeError):
            return self.reply(400, {})
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id,copies FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                number, copies = existing
            else:
                if db.execute('SELECT 1 FROM archived_jobs WHERE request_id=? LIMIT 1', (request_id,)).fetchone():
                    return self.reply(409, {'error': 'Dieser Testlauf ist beendet. Bitte die Seite neu öffnen.'})
                confession = text.strip()
                copies = int(setting(db, 'receipt_copies'))
                number = db.execute("INSERT INTO jobs(request_id,state,confession,created_at,copies,print_copies) VALUES (?,'queued',?,strftime('%Y-%m-%dT%H:%M:%SZ','now'),?,?)", (request_id, confession, copies, copies)).lastrowid
                db.execute('UPDATE jobs SET payload=? WHERE id=?', (render(number, confession), number))
        return self.reply(200, {'receiptNumber': f'{number:03d}', 'copies': copies})

    def claim(self, body):
        if body.get('protocol') != 2:
            return self.reply(426, {'error': 'Please update print_agent.py to the admin-features version.'})
        try:
            token = str(uuid.UUID(str(body.get('claimRequestId'))))
        except ValueError:
            return self.reply(400, {'error': 'claimRequestId must be a UUID.'})
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            promo = db.execute('SELECT id,payload,state FROM promo_jobs WHERE claim_token=?', (token,)).fetchone()
            if promo:
                if promo[2] != 'claimed':
                    return self.reply(409, {'error': 'This print attempt has already completed.'})
                return self.reply(200, {'id': -promo[0], 'payload': base64.b64encode(promo[1]).decode(), 'claimToken': token, 'copies': 1})
            existing = db.execute('SELECT id,payload,print_copies,state FROM jobs WHERE claim_token=?', (token,)).fetchone()
            if existing and existing[3] != 'claimed':
                return self.reply(409, {'error': 'This print attempt has already completed.'})
            job = existing[:3] if existing else None
            if not job and setting(db, 'paused') != '1':
                job = db.execute("SELECT id,payload,print_copies FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if not job and setting(db, 'paused') != '1':
                promo = db.execute("SELECT id,payload FROM promo_jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
                if promo:
                    db.execute("UPDATE promo_jobs SET state='claimed',claim_token=? WHERE id=?", (token, promo[0]))
                    # Negative transport IDs cannot consume or collide with confession IDs.
                    job = (-promo[0], promo[1], 1)
            if job and job[0] > 0:
                db.execute("UPDATE jobs SET state='claimed',claim_token=? WHERE id=?", (token, job[0]))
        # Each stored payload includes its own paper-cut command. Concatenation
        # keeps the existing protocol-2 laptop client compatible with 1 or 2 copies.
        return self.reply(200, {'id': job[0], 'payload': base64.b64encode(job[1] * job[2]).decode(), 'claimToken': token, 'copies': job[2]} if job else {})

    def finish_print(self, body):
        state, job_id, token = body.get('state'), body.get('id'), body.get('claimToken')
        if type(job_id) is not int or state not in ('sent', 'queued', 'uncertain') or not isinstance(token, str):
            return self.reply(400, {})
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            table = 'promo_jobs' if job_id < 0 else 'jobs'
            record_id = abs(job_id)
            job = db.execute(f'SELECT state,claim_token FROM {table} WHERE id=?', (record_id,)).fetchone()
            if not job or not hmac.compare_digest(token.encode(), (job[1] or '').encode()):
                return self.reply(409, {'error': 'Print attempt is no longer current. Inspect receipt in admin.'})
            if job[0] == 'claimed':
                db.execute(f'UPDATE {table} SET state=? WHERE id=?', (state, record_id))
            elif job[0] != state:
                return self.reply(409, {'error': 'Print attempt has already completed with another status.'})
        return self.reply(200, {})


if __name__ == '__main__':
    initialize()
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler).serve_forever()
