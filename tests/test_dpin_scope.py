# coding=utf-8
"""签名篡改、重放、应用边界与旧凭据路径；不连接收费模型。"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps'))
from django.conf import settings
if not settings.configured:
    settings.configure(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
from common.handle.dpin_scope import authenticated_dpin_scope
from common.exception.app_exception import AppAuthenticationFailed

SECRET = 'p7b-shared-test-identity-secret-0123456789'
APP = '01a0dbc2-4722-7681-9fbc-4ddf277cbbd6'
SCOPE = str(uuid.uuid5(uuid.NAMESPACE_URL, 'candidate-a'))


def signed_request(stamp=None):
    body = json.dumps({'dpin_scope': SCOPE, 'messages': [{'role': 'user', 'content': '合成问题'}]}).encode()
    stamp = str(int(time.time())) if stamp is None else stamp
    nonce = 'ab' * 16
    signed = '\n'.join(('dpin-maxkb-scope-v1', APP, SCOPE, stamp, nonce, hashlib.sha256(body).hexdigest()))
    return SimpleNamespace(body=body, headers={'X-DPIN-Scope': SCOPE, 'X-DPIN-Timestamp': stamp,
                                            'X-DPIN-Nonce': nonce,
                                            'X-DPIN-Signature': hmac.new(SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()})


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'MAXKB_DPIN_IDENTITY_SECRET': SECRET})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_valid_scope_and_replay(self):
        request = signed_request()
        with patch('common.handle.dpin_scope.cache.add', side_effect=[True, False]):
            self.assertEqual(authenticated_dpin_scope(request, APP), SCOPE)
            self.assertTrue(request.dpin_scope_verified)
            with self.assertRaises(AppAuthenticationFailed):
                authenticated_dpin_scope(request, APP)

    def test_tampering_expiry_and_other_application_fail_before_nonce_claim(self):
        for case in ('body', 'scope', 'signature', 'missing', 'expired', 'application'):
            with self.subTest(case=case):
                request = signed_request(str(int(time.time()) - 120) if case == 'expired' else None)
                app = APP
                if case == 'body':
                    request.body += b' '
                elif case == 'scope':
                    request.headers['X-DPIN-Scope'] = str(uuid.uuid5(uuid.NAMESPACE_URL, 'candidate-b'))
                elif case == 'signature':
                    request.headers['X-DPIN-Signature'] = '00' * 32
                elif case == 'missing':
                    del request.headers['X-DPIN-Nonce']
                elif case == 'application':
                    app = str(uuid.uuid4())
                with patch('common.handle.dpin_scope.cache.add') as claim:
                    with self.assertRaises(AppAuthenticationFailed):
                        authenticated_dpin_scope(request, app)
                    claim.assert_not_called()

    def test_ordinary_key_path_is_not_reinterpreted_as_dpin_identity(self):
        self.assertIsNone(authenticated_dpin_scope(SimpleNamespace(headers={}, body=b'{}'), APP))
        with self.assertRaises(AppAuthenticationFailed):
            authenticated_dpin_scope(SimpleNamespace(headers={}, body=json.dumps({'dpin_scope': SCOPE}).encode()), APP)


if __name__ == '__main__':
    unittest.main()
