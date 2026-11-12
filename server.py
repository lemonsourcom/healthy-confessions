import base64
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import uuid

from PIL import Image, ImageDraw, ImageFont, ImageOps

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
    body_font = ImageFont.truetype(str(FONT_DIR / 'LiberationSans-Italic.ttf'), 52)
    heading = ImageFont.truetype(str(FONT_DIR / 'LiberationSans-Bold.ttf'), 38)
    lines = []
    for paragraph in text.splitlines():
        line = ''
        for word in paragraph.split():
            candidate = (line + ' ' + word).strip()
            if body_font.getlength(candidate) <= 804:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ''
            for character in word:
                if body_font.getlength(line + character) > 804:
                    lines.append(line)
                    line = ''
                line += character
        lines.append(line)
    footer_y = 917 + (len(lines) - 2) * 64
    paper = Image.new('L', (945, footer_y + FOOT.height), 255)
    paper.paste(HEAD, (0, 0))
    paper.paste(FOOT, (0, footer_y))
    draw = ImageDraw.Draw(paper)
    draw.rectangle((197, 425, 748, 477), fill=255)
    label = f'CONFESSION NO. {number:03d}'
    advances = [heading.getlength(c) + 4.2 for c in label]
    x = (945 - sum(advances) + 4.2) / 2
    for character, advance in zip(label, advances):
        draw.text((x, 464), character, font=heading, fill=0, anchor='ls')
        x += advance
    for index, line in enumerate(lines):
        draw.text((79, 785 + index * 64), line, font=body_font, fill=0, anchor='ls')
    paper = paper.crop((48, 0, 897, paper.height))
    paper = paper.resize((576, round(paper.height * 576 / paper.width)), Image.Resampling.LANCZOS)
    raster = ImageOps.invert(paper).point(lambda p: 255 if p >= 128 else 0, '1')
    return b'\x1b@\x1dv0\x00' + struct.pack('<HH', 72, raster.height) + raster.tobytes() + b'\x1dVB\x00'


def connect():
    return sqlite3.connect(DB, timeout=10, isolation_level='IMMEDIATE')


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, data, mime='application/json'):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ('/admin', '/admin/'):
            return self.admin()
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/logo.png': ('assets/logo.png', 'image/png'),
                 '/forgiven.gif': ('assets/forgiven.gif', 'image/gif')}
        if self.path in files:
            name, mime = files[self.path]
            asset = ROOT / name
            if self.path == '/forgiven.gif' and not asset.is_file():
                asset, mime = ROOT / 'assets/forgiven.png', 'image/png'
            if not asset.is_file():
                return self.reply(404, {})
            self.reply(200, asset.read_bytes(), mime)
        else:
            self.reply(404, {})

    def admin(self):
        if len(ADMIN_PASSWORD) < 32:
            return self.reply(404, {})
        expected = base64.b64encode(f'admin:{ADMIN_PASSWORD}'.encode())
        supplied = self.headers.get('Authorization', '').encode()
        if not hmac.compare_digest(supplied, b'Basic ' + expected):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Healthy Confessions Admin"')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return

        with closing(connect()) as db:
            jobs = db.execute('SELECT id,confession,state FROM jobs ORDER BY id DESC').fetchall()
        rows = ''.join(
            f'<tr><th scope="row">{number:03d}</th>'
            f'<td>{escape(confession if confession is not None else "—")}</td>'
            f'<td>{escape(state)}</td></tr>'
            for number, confession, state in jobs
        )
        if not rows:
            rows = '<tr><td colspan="3">Noch keine Beichten.</td></tr>'
        page = f"""<!doctype html>
<html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Healthy Confessions · Intern</title>
<style>
body{{font:16px system-ui,sans-serif;color:#111;background:#fff;margin:24px auto;padding:0 16px;max-width:960px}}
h1{{font-size:26px}}table{{border-collapse:collapse;width:100%}}th,td{{text-align:left;vertical-align:top;padding:10px 8px;border-bottom:1px solid #ccc}}
thead th{{background:#111;color:white}}tbody td:nth-child(2){{white-space:pre-wrap;overflow-wrap:anywhere}}
</style>
<h1>Healthy Confessions · Intern</h1><p>{len(jobs)} Einträge</p>
<table><thead><tr><th scope="col">Bon</th><th scope="col">Beichte</th><th scope="col">Druckstatus</th></tr></thead>
<tbody>{rows}</tbody></table></html>"""
        return self.reply(200, page.encode(), 'text/html; charset=utf-8')

    def do_POST(self):
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                return self.reply(400, {})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                return self.reply(400, {})
        except (ValueError, UnicodeError):
            return self.reply(400, {})
        if self.path == '/api/confessions':
            return self.submit(body)
        if self.path not in ('/api/print/claim', '/api/print/finish'):
            return self.reply(404, {})
        auth = self.headers.get('Authorization', '').encode()
        if not hmac.compare_digest(auth, ('Bearer ' + TOKEN).encode()):
            return self.reply(401, {})
        if self.path.endswith('/claim'):
            return self.claim()
        return self.finish(body)

    def submit(self, body):
        text = body.get('confession')
        try:
            request_id = str(uuid.UUID(str(body.get('requestId'))))
            if not isinstance(text, str) or not text.strip() or len(text.encode('utf-16-le')) > 600:
                return self.reply(400, {})
        except ValueError:
            return self.reply(400, {})
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT id FROM jobs WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                number = existing[0]
            else:
                pending = db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','claimed')").fetchone()[0]
                if pending >= 40:
                    return self.reply(429, {})
                confession = text.strip()
                number = db.execute(
                    "INSERT INTO jobs(request_id,state,confession) VALUES (?,'queued',?)",
                    (request_id, confession),
                ).lastrowid
                db.execute('UPDATE jobs SET payload=? WHERE id=?', (render(number, confession), number))
        return self.reply(200, {'receiptNumber': f'{number:03d}'})

    def claim(self):
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            job = db.execute("SELECT id,payload FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if job:
                db.execute("UPDATE jobs SET state='claimed' WHERE id=?", (job[0],))
        return self.reply(200, {'id': job[0], 'payload': base64.b64encode(job[1]).decode()} if job else {})

    def finish(self, body):
        state = body.get('state')
        job_id = body.get('id')
        if type(job_id) is not int or state not in ('sent', 'queued', 'uncertain'):
            return self.reply(400, {})
        with closing(connect()) as db, db:
            if state == 'sent':
                db.execute("UPDATE jobs SET state=?,payload=NULL WHERE id=? AND state='claimed'", (state, job_id))
            else:
                db.execute("UPDATE jobs SET state=? WHERE id=? AND state='claimed'", (state, job_id))
        return self.reply(200, {})


if __name__ == '__main__':
    DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        db.execute(
            'CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, '
            'request_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, payload BLOB, confession TEXT)'
        )
        columns = {column[1] for column in db.execute('PRAGMA table_info(jobs)')}
        if 'confession' not in columns:
            db.execute('ALTER TABLE jobs ADD COLUMN confession TEXT')
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler).serve_forever()
