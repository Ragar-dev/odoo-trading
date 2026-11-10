"""Conexión en directo con el WebSocket de eToro: precios por instrumento y avisos de cartera.

Protocolo (https://api-portal.etoro.com/core/websocket/overview):

- ``wss://ws.etoro.com/ws``; tras conectar se envía ``Authenticate`` con ``userKey`` y ``apiKey``.
- ``Subscribe`` / ``Unsubscribe`` con ``topics``: ``instrument:<id>`` (precios) y ``private`` /
  ``private:demo`` (órdenes, aperturas y cierres de la cuenta real o demo).
- Lo que llega: ``{"messages": [{"topic", "type", "content"}]}``; ``content`` es JSON en texto.

La clase :class:`EtoroStreamProtocol` no toca la red (se prueba sin conexión).
:class:`EtoroStreamRunner` mantiene una conexión por cuenta, escribe en Odoo cada segundo y se
reconecta sola. Se arranca con ``odoo etoro_stream`` (ver ``cli/etoro_stream.py``).
"""
import json
import logging
import random
import threading
import time
import uuid

_logger = logging.getLogger(__name__)

WS_URL = 'wss://ws.etoro.com/ws'
RATE_TYPE = 'Trading.Instrument.Rate'

FLUSH_SECONDS = 1.0         # precios acumulados → Odoo
TOPICS_SECONDS = 60.0       # revisar qué instrumentos hay que seguir
HEARTBEAT_SECONDS = 30.0    # latido en la cuenta (estado «En directo»)
PING_SECONDS = 20.0         # ping del WebSocket
SYNC_DELAY_SECONDS = 2.0    # espera tras un aviso privado antes de sincronizar la cartera
AUTH_TIMEOUT_SECONDS = 15.0
MAX_BACKOFF_SECONDS = 60.0
AUTH_BACKOFF_SECONDS = 300.0


class StreamAuthError(Exception):
    """Claves rechazadas por eToro: no se reintenta enseguida."""


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class EtoroStreamProtocol:
    """Mensajes del WebSocket de eToro."""

    def __init__(self, api_key, user_key, environment):
        self.api_key = api_key
        self.user_key = user_key
        self.private_topic = 'private' if environment == 'real' else 'private:demo'

    @staticmethod
    def _request(operation, data):
        return json.dumps({'id': str(uuid.uuid4()), 'operation': operation, 'data': data})

    def auth_message(self):
        return self._request('Authenticate', {'userKey': self.user_key, 'apiKey': self.api_key})

    def subscribe_message(self, topics):
        return self._request('Subscribe', {'topics': sorted(topics), 'snapshot': True})

    def unsubscribe_message(self, topics):
        return self._request('Unsubscribe', {'topics': sorted(topics)})

    def parse(self, raw):
        """Mensaje recibido → ``{'rates': {broker_ref: rate}, 'private': [tipos], 'ack': (op, ok, error)}``.

        Lanza :class:`StreamAuthError` si la autenticación falla.
        """
        result = {'rates': {}, 'private': [], 'ack': None}
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', 'replace')
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return result
        if not isinstance(data, dict):
            return result
        if 'operation' in data and 'messages' not in data:
            ok = bool(data.get('success', True))
            error = data.get('errorMessage') or data.get('errorCode') or ''
            result['ack'] = (data.get('operation'), ok, error)
            if data.get('operation') == 'Authenticate' and not ok:
                raise StreamAuthError(f"eToro rechazó la autenticación del WebSocket: {error or 'sin detalle'}")
            return result
        for msg in data.get('messages') or []:
            if not isinstance(msg, dict):
                continue
            topic = msg.get('topic') or ''
            kind = msg.get('type') or ''
            content = msg.get('content')
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except ValueError:
                    content = {}
            if not isinstance(content, dict):
                content = {}
            if topic.startswith('instrument:') and kind in ('', RATE_TYPE):
                bid, ask = _num(content.get('Bid')), _num(content.get('Ask'))
                if bid or ask:
                    result['rates'][topic.partition(':')[2]] = {
                        'bid': bid, 'ask': ask,
                        'last': _num(content.get('LastExecution')) or None,
                        'date': content.get('Date'),
                    }
            elif topic.startswith('private'):
                result['private'].append(kind)
        return result


class EtoroStreamRunner:
    """Una conexión por cuenta de eToro, en su propio hilo.

    ``env_factory`` es un gestor de contexto que da un ``env`` de superusuario con su propio
    cursor y hace commit al salir (así los avisos del bus llegan a los navegadores).
    """

    def __init__(self, env_factory, account_id, connect=None, stop_event=None):
        self.env_factory = env_factory
        self.account_id = account_id
        self.connect = connect or self._connect
        self.stop_event = stop_event or threading.Event()
        self.protocol = None
        self.topics = set()
        self.pending_rates = {}
        self.sync_due = None

    # -- Odoo ---------------------------------------------------------------
    def load_config(self):
        """Claves, entorno y temas que seguir. None si la cuenta ya no debe ir en directo."""
        with self.env_factory() as env:
            acc = env['trading.broker.account'].browse(self.account_id).exists()
            if not acc or not acc.active or acc.broker_type != 'etoro' or not acc.etoro_stream \
                    or not acc.etoro_api_key or not acc.etoro_user_key:
                return None
            return {
                'protocol': EtoroStreamProtocol(acc.etoro_api_key, acc.etoro_user_key, acc.environment),
                'topics': acc._etoro_stream_topics(),
                'name': acc.name,
            }

    def flush(self, heartbeat=False):
        """Escribe los precios pendientes y, si toca, el latido. Si falla, se reintenta luego."""
        rates, self.pending_rates = self.pending_rates, {}
        if not rates and not heartbeat:
            return
        try:
            with self.env_factory() as env:
                acc = env['trading.broker.account'].browse(self.account_id)
                if rates:
                    instruments = env['trading.instrument'].search(
                        [('account_id', '=', acc.id), ('broker_ref', 'in', list(rates))])
                    instruments._apply_stream_rates(rates, acc._get_adapter())
                if heartbeat:
                    acc._stream_status('live')
        except Exception as e:  # noqa: BLE001 — un conflicto de escritura no debe tumbar el directo
            _logger.warning('Directo eToro: no se pudieron guardar %s precios: %s', len(rates), e)
            for ref, rate in rates.items():
                self.pending_rates.setdefault(ref, rate)

    def sync_portfolio(self):
        with self.env_factory() as env:
            env['trading.broker.account'].browse(self.account_id)._stream_portfolio_changed()

    def set_status(self, state, message=''):
        try:
            with self.env_factory() as env:
                env['trading.broker.account'].browse(self.account_id)._stream_status(state, message)
        except Exception as e:  # noqa: BLE001
            _logger.warning('Directo eToro: no se pudo guardar el estado: %s', e)

    # -- Red --------------------------------------------------------------
    @staticmethod
    def _connect():
        import websocket  # websocket-client; solo lo necesita el servicio del directo
        ws = websocket.create_connection(WS_URL, timeout=AUTH_TIMEOUT_SECONDS)
        return ws

    @staticmethod
    def _is_timeout(error):
        name = type(error).__name__
        return name in ('WebSocketTimeoutException', 'TimeoutError', 'timeout')

    def handle(self, raw):
        parsed = self.protocol.parse(raw)
        for ref, rate in parsed['rates'].items():
            self.pending_rates[ref] = rate
        if parsed['private']:
            _logger.info('Directo eToro: aviso de cartera %s', ', '.join(filter(None, parsed['private'])) or '-')
            self.sync_due = time.monotonic() + SYNC_DELAY_SECONDS
        return parsed

    def authenticate(self, ws):
        ws.send(self.protocol.auth_message())
        deadline = time.monotonic() + AUTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            parsed = self.handle(ws.recv())
            if parsed['ack'] and parsed['ack'][0] == 'Authenticate':
                return True
        raise TimeoutError('eToro no respondió a la autenticación del WebSocket.')

    def update_topics(self, ws, wanted):
        add, remove = wanted - self.topics, self.topics - wanted
        if remove:
            ws.send(self.protocol.unsubscribe_message(remove))
        if add:
            ws.send(self.protocol.subscribe_message(add))
        self.topics = set(wanted)

    def session(self, config):
        """Una conexión completa: autenticar, suscribir y escuchar hasta error o parada."""
        self.protocol = config['protocol']
        self.topics = set()
        ws = self.connect()
        try:
            self.authenticate(ws)
            self.update_topics(ws, config['topics'])
            ws.settimeout(1.0)
            self.set_status('live')
            _logger.info('Directo eToro: %s conectado, %s temas.', config['name'], len(self.topics))
            now = time.monotonic()
            next_flush = now + FLUSH_SECONDS
            next_topics = now + TOPICS_SECONDS
            next_beat = now + HEARTBEAT_SECONDS
            next_ping = now + PING_SECONDS
            while not self.stop_event.is_set():
                try:
                    self.handle(ws.recv())
                except Exception as e:  # noqa: BLE001
                    if not self._is_timeout(e):
                        raise
                now = time.monotonic()
                if now >= next_ping:
                    ws.ping()
                    next_ping = now + PING_SECONDS
                if now >= next_flush:
                    beat = now >= next_beat
                    self.flush(heartbeat=beat)
                    next_flush = now + FLUSH_SECONDS
                    if beat:
                        next_beat = now + HEARTBEAT_SECONDS
                if self.sync_due and now >= self.sync_due:
                    self.sync_due = None
                    self.sync_portfolio()
                if now >= next_topics:
                    fresh = self.load_config()
                    if fresh is None:
                        return
                    self.update_topics(ws, fresh['topics'])
                    next_topics = now + TOPICS_SECONDS
        finally:
            self.flush()
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def run(self):
        attempt = 0
        while not self.stop_event.is_set():
            try:
                config = self.load_config()
                if config is None:
                    self.set_status('off', 'Directo desactivado o sin claves.')
                    return
                started = time.monotonic()
                self.session(config)
                if self.stop_event.is_set():
                    break
                attempt = 0 if time.monotonic() - started > 60 else attempt + 1
                delay = min(MAX_BACKOFF_SECONDS, 2 ** attempt)
                self.set_status('error', 'Conexión cerrada por eToro; reconectando.')
            except StreamAuthError as e:
                _logger.error('Directo eToro: %s', e)
                self.set_status('error', str(e))
                delay = AUTH_BACKOFF_SECONDS
            except Exception as e:  # noqa: BLE001
                attempt += 1
                delay = min(MAX_BACKOFF_SECONDS, 2 ** attempt)
                _logger.warning('Directo eToro: %s; reintento en %.0f s.', e, delay)
                self.set_status('error', f'{type(e).__name__}: {e}'[:250])
            self.stop_event.wait(delay + random.random())
        self.set_status('off', 'Servicio parado.')
