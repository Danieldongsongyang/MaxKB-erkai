import os
from pathlib import Path

from celery.signals import heartbeat_sent, worker_ready, worker_shutdown


RUNTIME_TMP_DIR = Path(os.environ.get('MAXKB_TMP_DIR', os.environ.get('TMPDIR', '/opt/maxkb-app/tmp')))


@heartbeat_sent.connect
def heartbeat(sender, **kwargs):
    worker_name = sender.eventer.hostname.split('@')[0]
    heartbeat_path = RUNTIME_TMP_DIR / 'worker_heartbeat_{}'.format(worker_name)
    heartbeat_path.touch()


@worker_ready.connect
def worker_ready(sender, **kwargs):
    worker_name = sender.hostname.split('@')[0]
    ready_path = RUNTIME_TMP_DIR / 'worker_ready_{}'.format(worker_name)
    ready_path.touch()


@worker_shutdown.connect
def worker_shutdown(sender, **kwargs):
    worker_name = sender.hostname.split('@')[0]
    for signal in ['ready', 'heartbeat']:
        path = RUNTIME_TMP_DIR / 'worker_{}_{}'.format(signal, worker_name)
        path.unlink(missing_ok=True)
