"""Authentication, bounded requests, and payload-free telemetry."""
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from collections import Counter, deque

from fastapi import Header, HTTPException
from starlette.responses import JSONResponse


class Security:
    def __init__(self, keys=None, rpm=120):
        self.keys = keys if keys is not None else json.loads(os.getenv('TENANT_KEYS', '{}'))
        if not self.keys or any(not isinstance(k, str) or len(k) < 24 or
                                not isinstance(v, str) or not v for k, v in self.keys.items()):
            raise ValueError('TENANT_KEYS must map API keys of at least 24 characters to tenant IDs')
        self.rpm = rpm
        self.windows = {}
        self.lock = threading.Lock()

    def tenant(self, x_api_key: str = Header(default='')) -> str:
        tenant = None
        for key, value in self.keys.items():
            if hmac.compare_digest(hashlib.sha256(x_api_key.encode()).digest(),
                                   hashlib.sha256(key.encode()).digest()):
                tenant = value
        if tenant is None:
            raise HTTPException(401, 'Invalid API key')
        now = time.monotonic()
        with self.lock:
            window = self.windows.setdefault(tenant, deque())
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.rpm:
                raise HTTPException(429, 'Rate limit exceeded', headers={'Retry-After': '60'})
            window.append(now)
        return tenant


class Telemetry:
    """Single-process counters. Never records messages, keys, document IDs or tenants."""
    def __init__(self):
        self.counts = Counter()
        self.lock = threading.Lock()

    def increment(self, name):
        with self.lock:
            self.counts[name] += 1

    def snapshot(self):
        with self.lock:
            return dict(self.counts)


class BoundedRequests:
    """Bound actual bytes, including chunked bodies, before JSON parsing."""
    def __init__(self, app, limit=1_048_576):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            body.extend(message.get('body', b''))
            if len(body) > self.limit:
                return await JSONResponse({'detail': 'Request too large'}, status_code=413)(scope, receive, send)
            if not message.get('more_body', False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
            return await receive()

        request_id = str(uuid.uuid4())

        async def secure_send(message):
            if message['type'] == 'http.response.start':
                headers = list(message.get('headers', []))
                headers.extend([(b'x-request-id', request_id.encode()),
                                (b'x-content-type-options', b'nosniff'),
                                (b'cache-control', b'no-store')])
                message['headers'] = headers
            await send(message)

        await self.app(scope, replay, secure_send)
