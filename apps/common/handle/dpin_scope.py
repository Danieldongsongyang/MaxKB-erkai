# coding=utf-8
"""D-PIN 服务签名、隔离身份和跨进程的同作用域串行执行。"""
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

import psycopg
from django.core.cache import cache
from django.db import connection
from common.exception.app_exception import AppAuthenticationFailed, AppApiException

class DpinConversationConflict(AppApiException):
    status_code = 409


HEADERS = ('X-DPIN-Scope', 'X-DPIN-Timestamp', 'X-DPIN-Nonce', 'X-DPIN-Signature')


def authenticated_dpin_scope(request, application_id):
    values = [request.headers.get(name, '') for name in HEADERS]
    if not any(values):
        try:
            body = json.loads(request.body) if request.body else {}
            if isinstance(body, dict) and 'dpin_scope' in body:
                raise AppAuthenticationFailed(401, 'D-PIN 会话身份缺少服务签名')
        except (ValueError, AttributeError):
            pass
        return None
    scope, stamp, nonce, signature = values
    secret = os.getenv('MAXKB_DPIN_IDENTITY_SECRET', '')
    if not secret:
        path = Path(__file__).resolve().parents[3] / '.runtime/dpin-identity-secret'
        if path.is_file():
            secret = path.read_text().strip()
    invalid = AppAuthenticationFailed(401, 'D-PIN 会话身份签名无效或已过期')
    if len(secret) < 32 or not re.fullmatch(r'[0-9]{10}', stamp):
        raise invalid
    try:
        parsed = uuid.UUID(scope)
        if str(parsed) != scope or parsed.version != 5:
            raise invalid
        body = request.body
        if json.loads(body).get('dpin_scope') != scope:
            raise invalid
    except (ValueError, TypeError, AttributeError):
        raise invalid from None
    if abs(time.time() - int(stamp)) > 60 or not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise invalid
    if not re.fullmatch(r'[0-9a-f]{64}', signature):
        raise invalid
    payload = '\n'.join(('dpin-maxkb-scope-v1', str(application_id), scope, stamp, nonce,
                         hashlib.sha256(body).hexdigest()))
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise invalid
    # 验签成功后才占用 nonce，修改正文、换应用和重放均不进入模型调用。
    replay_key = 'dpin-scope-nonce:' + hashlib.sha256((str(application_id) + nonce).encode()).hexdigest()
    if not cache.add(replay_key, True, timeout=121):
        raise invalid
    request.dpin_scope_verified = True
    return scope


@contextmanager
def serialized_dpin_scope(application_id, scope):
    """独立 PostgreSQL 会话持锁，不持有跨模型调用的业务事务；进程退出自动释放。

    MaxKB 本身使用 PostgreSQL。独立连接避免原模型工具归还 Django 池连接时释放锁。
    没有 Redis 租约过期后并行提交的窗口，同一记忆作用域的多个 chat_id 也串行。
    """
    settings = connection.settings_dict
    lock_id = int.from_bytes(hashlib.sha256((str(application_id) + ':' + scope).encode()).digest()[:8],
                             'big', signed=True)
    with psycopg.connect(dbname=settings['NAME'], host=settings['HOST'], port=settings['PORT'],
                         user=settings['USER'], password=settings['PASSWORD'],
                         connect_timeout=5, autocommit=True) as lock_connection:
        deadline = time.monotonic() + 20
        with lock_connection.cursor() as cursor:
            while True:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', (lock_id,))
                if cursor.fetchone()[0]:
                    break
                if time.monotonic() >= deadline:
                    raise DpinConversationConflict(409, '同一应聘者的对话正在处理，请稍后重试')
                time.sleep(.05)
        try:
            yield
        finally:
            with lock_connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', (lock_id,))
