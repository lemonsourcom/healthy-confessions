import base64
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/logo.png': ('assets/logo.png', 'image/png')}
        if self.path in files:
            name, mime = files[self.path]
            self.reply(200, (ROOT / name).read_bytes(), mime)
        else:
            self.reply(404, {})

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
                number = db.execute("INSERT INTO jobs(request_id,state) VALUES (?,'queued')", (request_id,)).lastrowid
                db.execute('UPDATE jobs SET payload=? WHERE id=?', (render(number, text.strip()), number))
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
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, payload BLOB)')
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler).serve_forever()
