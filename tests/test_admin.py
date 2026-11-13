import base64
from concurrent.futures import ThreadPoolExecutor
import csv
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['PRINT_TOKEN'] = secrets.token_hex(32)
os.environ['ADMIN_PASSWORD'] = secrets.token_hex(32)
import server
import print_agent

class QuietHandler(server.Handler):
    def log_message(self, *args): pass

class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.http.server_port}'
        cls.admin = {'Authorization': 'Basic ' + base64.b64encode(('admin:' + server.ADMIN_PASSWORD).encode()).decode()}
        cls.printer = {'Authorization': 'Bearer ' + server.TOKEN}
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join()
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DB = Path(self.tmp.name) / 'test.sqlite3'
        server.initialize()
        self.addCleanup(self.tmp.cleanup)
    def req(self, path, body=None, headers=None):
        request = Request(self.base + path, None if body is None else json.dumps(body).encode(), {**(headers or {}), 'Content-Type': 'application/json'})
        try: response = urlopen(request, timeout=20)
        except HTTPError as error: response = error
        with response:
            content = response.read()
            return response.status, json.loads(content) if response.headers.get_content_type() == 'application/json' else content
    def status(self):
        code, body = self.req('/admin/api/state', headers=self.admin)
        self.assertEqual(code, 200)
        return body
    def act(self, name, data, csrf=None):
        return self.req('/admin/api/' + name, data, {**self.admin, 'X-CSRF-Token': csrf or self.status()['csrf']})
    def submit(self, text='I said I was going home. I went dancing.', request_id=None):
        return self.req('/api/confessions', {'confession': text, 'requestId': request_id or str(uuid.uuid4())})
    def claim(self, request_id=None):
        return self.req('/api/print/claim', {'protocol': 2, 'claimRequestId': request_id or str(uuid.uuid4())}, self.printer)
    def finish(self, job, state='sent'):
        return self.req('/api/print/finish', {'id': job['id'], 'claimToken': job['claimToken'], 'state': state}, self.printer)

    def test_auth_and_csrf(self):
        for path in ['/admin','/admin/admin.js','/admin/api/state','/admin/export.csv','/admin/export-all.csv','/admin/receipt/1.png']:
            self.assertEqual(self.req(path)[0],401)
        for action in ['pause','reset','reprint']:
            self.assertEqual(self.req('/admin/api/'+action,{})[0],401)
            self.assertEqual(self.req('/admin/api/'+action,{},self.admin)[0],403)
        self.assertEqual(self.req('/api/print/claim',{})[0],401)
        self.assertEqual(self.req('/api/print/claim',{},self.printer)[0],426)
        self.assertEqual(self.req('/admin',headers=self.admin)[0],200)
    def test_pause_keeps_storing(self):
        self.assertEqual(self.act('pause',{'paused':True})[0],200)
        self.assertEqual(self.submit()[1]['receiptNumber'],'001')
        self.assertEqual(self.claim()[1],{})
        self.act('pause',{'paused':False})
        self.assertEqual(self.claim()[1]['id'],1)
    def test_preview_and_reprint_same_number(self):
        self.submit('Hello <script>alert(1)</script> & friends.\nSecond line.')
        original=self.claim()[1]; self.finish(original)
        png=self.req('/admin/receipt/1.png',headers=self.admin)
        self.assertEqual(png[0],200)
        self.assertEqual(png[1],server.preview_bytes(base64.b64decode(original['payload'])))
        self.assertEqual(self.act('reprint',{'id':1,'revision':0})[0],200)
        self.assertEqual(self.act('reprint',{'id':1,'revision':0})[0],409)
        second=self.claim()[1]
        self.assertEqual(second['payload'],original['payload'])
        self.assertEqual(second['id'],1)
        self.assertNotEqual(second['claimToken'],original['claimToken'])
        self.assertEqual(self.finish(original)[0],409)
        self.finish(second)
        self.assertEqual(self.act('reprint',{'id':1,'revision':0})[0],409)
        self.assertEqual(self.status()['nextNumber'],'002')
        self.assertEqual(self.status()['jobs'][0]['reprints'],1)
    def test_claim_finish_idempotency(self):
        self.submit(); self.submit('Another one')
        token=str(uuid.uuid4()); first=self.claim(token)[1]
        self.assertEqual(self.claim(token)[1],first)
        self.assertEqual(self.finish(first)[0],200)
        self.assertEqual(self.finish(first)[0],200)
        self.assertEqual(self.claim(token)[0],409)
        self.assertEqual(self.claim()[1]['id'],2)
    def test_reset_archive_and_stale_requests(self):
        request_id=str(uuid.uuid4()); self.submit(request_id=request_id)
        old_job=self.claim()[1]; self.finish(old_job); old=self.status()
        self.assertEqual(self.act('reset',{'confirmation':'RESET 001'})[0],409)
        self.act('pause',{'paused':True})
        self.assertEqual(self.act('reset',{'confirmation':'wrong'})[0],400)
        self.assertEqual(self.act('reset',{'confirmation':'RESET 001'})[0],200)
        self.assertTrue(self.status()['paused'])
        self.assertEqual(self.status()['nextNumber'],'001')
        self.assertEqual(self.status()['jobs'],[])
        self.assertEqual(self.act('reset',{'confirmation':'RESET 001'},old['csrf'])[0],403)
        self.assertEqual(self.submit(request_id=request_id)[0],409)
        self.assertEqual(self.submit('New event')[1]['receiptNumber'],'001')
        self.assertEqual(self.req('/admin/receipt/1.png?run='+old['runId'],headers=self.admin)[0],409)
        self.act('pause',{'paused':False}); new_job=self.claim()[1]
        self.assertEqual(self.finish(old_job)[0],409)
        self.assertEqual(self.status()['jobs'][0]['state'],'claimed')
        self.finish(new_job)
        rows=list(csv.reader(io.StringIO(self.req('/admin/export-all.csv',headers=self.admin)[1].decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(len(rows),3)
        self.assertEqual([r[1] for r in rows[1:]],['001','001'])
        self.assertNotEqual(rows[1][0],rows[2][0])
        server.initialize()
        self.assertEqual(self.status()['jobs'][0]['confession'],'New event')
    def test_reset_blocks_running_job(self):
        self.submit(); job=self.claim()[1]; self.act('pause',{'paused':True})
        self.assertEqual(self.act('reset',{'confirmation':'RESET 001'})[0],409)
        self.assertEqual(self.claim(job['claimToken'])[1],job)
        self.finish(job,'uncertain')
        self.assertEqual(self.act('reset',{'confirmation':'RESET 001'})[0],200)
    def test_csv_unicode_newlines_and_formula_guard(self):
        text='=HYPERLINK("bad"); Grüße\n乾杯'; self.submit(text)
        data=self.req('/admin/export.csv',headers=self.admin)[1]
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.reader(io.StringIO(data.decode('utf-8-sig')),delimiter=';'))
        self.assertEqual(rows[1][1],'001'); self.assertEqual(rows[1][2],"'"+text)
        self.assertEqual(self.status()['jobs'][0]['confession'],text)
    def test_legacy_migration(self):
        server.DB.unlink(); payload=server.render(4,'Legacy queued confession')
        with sqlite3.connect(server.DB) as db:
            db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT UNIQUE NOT NULL,state TEXT NOT NULL,payload BLOB)')
            db.execute("INSERT INTO jobs VALUES (3,?,'sent',NULL)",(str(uuid.uuid4()),))
            db.execute("INSERT INTO jobs VALUES (4,?,'claimed',?)",(str(uuid.uuid4()),payload))
        server.initialize(); server.initialize(); status=self.status()
        self.assertEqual(status['nextNumber'],'005'); self.assertEqual(status['jobs'][0]['state'],'uncertain')
        self.assertFalse(status['jobs'][1]['canPreview'])
        self.assertEqual(self.req('/admin/receipt/4.png',headers=self.admin)[1],server.preview_bytes(payload))
        self.assertEqual(self.act('reprint',{'id':3,'revision':0})[0],409)
        self.assertEqual(self.act('reprint',{'id':4,'revision':0})[0],200)
    def test_concurrent_numbering_and_duplicate_submit(self):
        token=str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _: self.submit(request_id=token),range(6)))
        self.assertTrue(all(r==(200,{'receiptNumber':'001'}) for r in results))
        with ThreadPoolExecutor(max_workers=6) as pool:
            results=list(pool.map(lambda _: self.submit(),range(6)))
        self.assertEqual({r[1]['receiptNumber'] for r in results},{f'{i:03d}' for i in range(2,8)})
    def test_multilingual_storage_preview_and_reprint(self):
        texts = ['家に帰ると言いました。でも、朝まで踊っていました。',
                 'قلت إنني سأعود إلى المنزل، لكنني بقيت أرقص حتى الصباح.',
                 'اعترف: I stayed for 3 more songs. 乾杯！',
                 'Ich ging tanzen 😂🍋. Verzeih mir 🙏🏽❤️ 👩‍💻 🇯🇵 1️⃣']
        for number, text in enumerate(texts, 1):
            self.assertEqual(self.submit(text)[1]['receiptNumber'], f'{number:03d}')
            self.assertEqual(self.status()['jobs'][0]['confession'], text)
            code, png = self.req(f'/admin/receipt/{number}.png', headers=self.admin)
            self.assertEqual(code, 200)
            self.assertTrue(png.startswith(b'\x89PNG'))
            job = self.claim()[1]; self.finish(job)
            self.assertEqual(self.act('reprint', {'id': number, 'revision': 0})[0], 200)
            repeat = self.claim()[1]
            self.assertEqual(repeat['payload'], job['payload'])
            self.finish(repeat)
        csv_data = self.req('/admin/export.csv', headers=self.admin)[1].decode('utf-8-sig')
        for text in texts:
            self.assertIn(text, csv_data)

    def test_nul_rejected_without_consuming_number(self):
        self.assertEqual(self.submit('visible\x00hidden')[0], 400)
        self.assertEqual(self.status()['nextNumber'], '001')

    def test_character_limit(self):
        for text,expected in [('a'*300,200),('a'*301,400),('😀'*150,200),('😀'*151,400),('\ud800',400),('   ',400)]:
            self.assertEqual(self.submit(text)[0],expected)
    def run_agent(self, connection, lose_finish=False, lose_claim=False):
        lost=set()
        def forward(request,**kwargs):
            path=request.full_url.removeprefix('https://test.example')
            response=urlopen(Request(self.base+path,request.data,dict(request.header_items())),**kwargs)
            kind='finish' if path.endswith('/finish') else 'claim'
            if kind not in lost and ((kind=='finish' and lose_finish) or (kind=='claim' and lose_claim)):
                response.read(); response.close(); lost.add(kind)
                raise URLError('Simulated response loss after server commit')
            return response
        def sleep(seconds):
            if seconds==1: raise StopIteration('Queue drained')
        with patch('builtins.input',side_effect=['https://test.example','192.0.2.1']), patch.object(print_agent.getpass,'getpass',return_value=server.TOKEN), patch.object(print_agent,'__file__',str(Path(self.tmp.name)/'print_agent.py')), patch.object(print_agent,'urlopen',side_effect=forward), patch.object(print_agent,'socket',SimpleNamespace(create_connection=MagicMock(return_value=connection))), patch.object(print_agent.time,'sleep',side_effect=sleep):
            try: print_agent.main()
            except StopIteration: pass
    def test_client_lost_responses_no_double_print(self):
        self.submit(); connection=MagicMock()
        self.run_agent(connection,lose_finish=True,lose_claim=True)
        self.assertEqual(connection.__enter__.return_value.sendall.call_count,1)
        self.assertEqual(self.status()['jobs'][0]['state'],'sent')
        self.assertEqual(list(Path(self.tmp.name).glob('print-pending-*.json')),[])
    def test_client_crash_recovery_no_retransmission(self):
        self.submit(); connection=MagicMock()
        connection.__enter__.return_value.sendall.side_effect=KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt): self.run_agent(connection)
        self.assertEqual(self.status()['jobs'][0]['state'],'claimed')
        connection2=MagicMock()
        with self.assertRaises(SystemExit): self.run_agent(connection2)
        self.assertEqual(connection2.__enter__.return_value.sendall.call_count,0)
        self.assertEqual(self.status()['jobs'][0]['state'],'uncertain')
        self.assertEqual(list(Path(self.tmp.name).glob('print-pending-*.json')),[])

if __name__=='__main__': unittest.main(verbosity=2)
