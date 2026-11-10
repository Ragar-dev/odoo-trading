"""``odoo etoro_stream``: precios y avisos de cartera de eToro en directo, por WebSocket.

Es un proceso aparte del servidor web (Odoo no mantiene conexiones abiertas a terceros).
Lee las claves de la base de datos, escribe los precios cada segundo y avisa a los navegadores
por el bus de Odoo. Necesita ``websocket-client`` (paquete Debian ``python3-websocket``).
"""
import contextlib
import logging
import signal
import threading

from odoo import api
from odoo.cli.command import Command
from odoo.modules.registry import Registry
from odoo.service import server
from odoo.tools import config

_logger = logging.getLogger('odoo.addons.trading_etoro.stream')

SUPERVISE_SECONDS = 60


class EtoroStream(Command):
    """Precios y cartera de eToro en directo (WebSocket)."""
    name = 'etoro_stream'

    def run(self, args):
        config.parse_config(args, setup_logging=True)
        server.start(preload=[], stop=True)
        dbs = config['db_name']
        dbname = (dbs[0] if isinstance(dbs, (list, tuple)) and dbs else dbs) or 'trading'
        threading.current_thread().dbname = dbname

        from odoo.addons.trading_etoro.stream import EtoroStreamRunner  # noqa: PLC0415

        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *a: stop.set())
        signal.signal(signal.SIGINT, lambda *a: stop.set())

        @contextlib.contextmanager
        def env_factory():
            registry = Registry(dbname).check_signaling()
            with registry.cursor() as cr:
                yield api.Environment(cr, api.SUPERUSER_ID, {})

        threads = {}
        _logger.info('Directo eToro: arrancado sobre la base %s.', dbname)
        while not stop.is_set():
            try:
                with env_factory() as env:
                    account_ids = env['trading.broker.account'].search([
                        ('broker_type', '=', 'etoro'), ('etoro_stream', '=', True)]).ids
            except Exception as e:  # noqa: BLE001
                _logger.warning('Directo eToro: no se pudieron leer las cuentas: %s', e)
                account_ids = []
            for account_id in account_ids:
                thread = threads.get(account_id)
                if thread is None or not thread.is_alive():
                    runner = EtoroStreamRunner(env_factory, account_id, stop_event=stop)
                    thread = threading.Thread(target=runner.run, name=f'etoro-stream-{account_id}', daemon=True)
                    thread.dbname = dbname
                    thread.start()
                    threads[account_id] = thread
            stop.wait(SUPERVISE_SECONDS)
        for thread in threads.values():
            thread.join(timeout=10)
        _logger.info('Directo eToro: parado.')
