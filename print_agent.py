import base64
import getpass
import json
import socket
import time
from urllib.request import Request, urlopen


def main():
    url = input('Railway HTTPS URL: ').strip().rstrip('/')
    if not url.startswith('https://'):
        raise SystemExit('Use the Railway HTTPS URL.')
    printer = input('Epson LAN IP: ').strip()
    token = getpass.getpass('PRINT_TOKEN: ')

    def api(action, data):
        request = Request(url + '/api/print/' + action, json.dumps(data).encode(),
                          {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    while True:
        try:
            job = api('claim', {})
            if not job:
                time.sleep(1)
                continue
            state = 'queued'
            try:
                with socket.create_connection((printer, 9100), timeout=5) as connection:
                    state = 'uncertain'
                    connection.sendall(base64.b64decode(job['payload']))
                    state = 'sent'
            except OSError as error:
                print(error)
            # Retry only this acknowledgement after a cloud connection error.
            while True:
                try:
                    api('finish', {'id': job['id'], 'state': state})
                    break
                except OSError as error:
                    print('Confirmation pending:', error)
                    time.sleep(3)
            print(f"Bon {job['id']:03d}: {state}", flush=True)
            if state == 'uncertain':
                print('Check the physical receipt before continuing.')
                return
            if state == 'queued':
                time.sleep(3)
        except OSError as error:
            print(error)
            time.sleep(3)


if __name__ == '__main__':
    main()
