"""Local LAN print client, protocol 2. Run one client per Epson printer."""
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    url = input('Railway HTTPS URL: ').strip().rstrip('/')
    if not url.startswith('https://'):
        raise SystemExit('Use the Railway HTTPS URL.')
    printer = input('Epson LAN IP: ').strip()
    token = getpass.getpass('PRINT_TOKEN: ')
    key = hashlib.sha256((url + '|' + printer).encode()).hexdigest()[:16]
    journal = Path(__file__).resolve().parent / f'print-pending-{key}.json'

    def save(data):
        temporary = journal.with_suffix('.tmp')
        with temporary.open('w') as output:
            os.chmod(temporary, 0o600)
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(journal)

    def api(action, data):
        request = Request(url + '/api/print/' + action, json.dumps(data).encode(),
                          {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        while True:
            try:
                with urlopen(request, timeout=10) as response:
                    return json.load(response)
            except HTTPError as error:
                if 400 <= error.code < 500:
                    detail = error.read().decode(errors='replace')
                    raise SystemExit(f'Server rejected request ({error.code}): {detail}\n'
                                     'Client stopped. Check admin before restarting.\n'
                                     f'If a pending file exists, keep it for recovery: {journal.name}')
                print(f'Server error {error.code}; retrying the same request.', flush=True)
            except OSError as error:
                print('Connection error; retrying the same request:', error, flush=True)
            time.sleep(3)

    def acknowledge(pending):
        api('finish', {key: pending[key] for key in ('id', 'state', 'claimToken')})
        journal.unlink(missing_ok=True)
        print(f"Bon {pending['id']:03d}: {pending['state']}", flush=True)
        if pending['state'] == 'uncertain':
            raise SystemExit('Check the physical receipt. Use Nachdrucken in admin if needed, then restart this client.')
        if pending['state'] == 'queued':
            time.sleep(3)

    while True:
        pending = json.loads(journal.read_text()) if journal.exists() else None
        if pending and pending['phase'] == 'ack':
            acknowledge(pending)
            continue
        if not pending:
            pending = {'phase': 'claiming', 'claimRequestId': str(uuid.uuid4())}
            save(pending)
        job = api('claim', {'protocol': 2, 'claimRequestId': pending['claimRequestId']})
        if not job:
            journal.unlink(missing_ok=True)
            time.sleep(1)
            continue
        pending = {'phase': 'ack', 'id': job['id'], 'claimToken': job['claimToken'], 'state': 'queued'}
        save(pending)
        payload = base64.b64decode(job['payload'], validate=True)
        try:
            with socket.create_connection((printer, 9100), timeout=5) as connection:
                pending['state'] = 'uncertain'
                save(pending)
                connection.sendall(payload)
                pending['state'] = 'sent'
                save(pending)
        except OSError as error:
            print('Printer:', error, flush=True)
        acknowledge(pending)


if __name__ == '__main__':
    main()
