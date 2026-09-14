#!/usr/bin/env python3
"""Exercise deployed API; leaves one measurement/photo with source acceptance.

Credentials are read from files and never printed. Run with a non-sensitive JPEG.
"""
import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(url, device_id, device_token, admin_key, photo):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('API URL must be an HTTPS origin without credentials')
    if not 4 <= len(photo) <= 4 * 1024 * 1024 or not photo.startswith(b'\xff\xd8') or not photo.endswith(b'\xff\xd9'):
        raise ValueError('Expected a JPEG up to 4 MiB')
    opener = build_opener(NoRedirect())

    def request(path, method='GET', token=None, payload=None, data=None, headers=None, expected=200):
        headers = dict(headers or {})
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if payload is not None:
            data = json.dumps(payload).encode()
            headers['Content-Type'] = 'application/json'
        req = Request(url.rstrip('/') + path, data=data, method=method, headers=headers)
        try:
            response = opener.open(req, timeout=60)
        except HTTPError as error:
            response = error
        with response:
            result = response.read()
            if response.status != expected:
                # Do not echo server content or credentials into logs.
                raise RuntimeError(f'{method} {path}: HTTP {response.status}, expected {expected}')
            if response.headers.get('Content-Type', '').startswith('application/json'):
                return json.loads(result)
            return result

    def require(condition, message):
        if not condition:
            raise RuntimeError(message)

    require(request('/healthz') == {'status': 'ok'}, 'Health check failed')
    request('/v1/latest', expected=401)
    request('/v1/latest', token=device_token, expected=401)
    session = request('/v1/session', method='POST', payload={'key': admin_key})['session_key']
    event = dict(schema_version=1, device_id=device_id, event_id=str(uuid.uuid4()),
                 observed_at=time.time(), kind='measurement', source='acceptance',
                 status='ok', values={'test_value': 1}, clock_synchronized=True)
    image_event = {**event, 'kind': 'photo', 'event_id': str(uuid.uuid4()), 'values': {}}
    try:
        request('/v1/measurements', method='POST', token=session, payload={'events': [event]}, expected=401)
        for status in ('stored', 'duplicate'):
            ack = request('/v1/measurements', method='POST', token=device_token, payload={'events': [event]})
            require(ack == {'results': [{'event_id': event['event_id'], 'status': status}]}, 'Measurement ACK mismatch')
            ack = request('/v1/photos', method='POST', token=device_token, data=photo,
                          headers={'Content-Type': 'image/jpeg', 'X-Event': json.dumps(image_event)})
            require(ack == {'event_id': image_event['event_id'], 'status': status}, 'Photo ACK mismatch')
        path = '/v1/photos/' + image_event['event_id']
        request(path, expected=401)
        require(request(path, token=session) == photo, 'Downloaded JPEG differs')
        for kind, item in [('measurements', event), ('photos', image_event)]:
            page = request(f'/v1/{kind}?source=acceptance&start={event["observed_at"]}&limit=1000', token=session)
            require(sum(x['event_id'] == item['event_id'] for x in page['items']) == 1, 'Event missing or duplicated')
    finally:
        request('/v1/session', method='DELETE', token=session, expected=204)
    request('/v1/latest', token=session, expected=401)
    print('PASS: health, auth, uploads, duplicate ACKs, private JPEG, history, logout')
    print('measurement event_id:', event['event_id'])
    print('photo event_id:', image_event['event_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--device-id', default='home')
    parser.add_argument('--device-token', type=Path, required=True)
    parser.add_argument('--admin-key', type=Path, required=True)
    parser.add_argument('--photo', type=Path, required=True)
    args = parser.parse_args()
    check(args.url, args.device_id, args.device_token.read_text().strip(),
          args.admin_key.read_text().strip(), args.photo.read_bytes())


if __name__ == '__main__':
    main()
