import contextlib
import json
import threading
from unittest.mock import patch

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.trading_etoro.stream import EtoroStreamProtocol, EtoroStreamRunner, StreamAuthError


def _rate_msg(ref, bid, ask, last=None):
    content = {'Ask': str(ask), 'Bid': str(bid), 'Date': '2026-11-10T08:36:02.8305456Z', 'PriceRateID': '1'}
    if last is not None:
        content['LastExecution'] = str(last)
    return json.dumps({'messages': [{'topic': f'instrument:{ref}', 'content': json.dumps(content),
                                     'id': 'x', 'type': 'Trading.Instrument.Rate'}]})


class FakeWs:
    """WebSocket de prueba: devuelve los mensajes en cola y después para el runner."""

    def __init__(self, messages, stop):
        self.messages = list(messages)
        self.sent = []
        self.stop = stop
        self.closed = False

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self):
        if self.messages:
            return self.messages.pop(0)
        self.stop.set()
        raise TimeoutError()

    def settimeout(self, value):
        pass

    def ping(self):
        pass

    def close(self):
        self.closed = True


@tagged('post_install', '-at_install')
class TestEtoroStream(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['trading.broker.account'].create({
            'name': 'eToro demo', 'broker_type': 'etoro', 'environment': 'demo',
            'etoro_api_key': 'API', 'etoro_user_key': 'USER',
        })
        cls.instrument = cls.env['trading.instrument'].create({
            'account_id': cls.account.id, 'broker_ref': '1001', 'symbol': 'AAPL', 'asset_type': 'stock',
            'prev_close': 100.0, 'prev_close_date': fields.Date.today()})
        cls.position = cls.env['trading.position'].create({
            'account_id': cls.account.id, 'instrument_id': cls.instrument.id, 'broker_ref': '555',
            'open_rate': 100.0, 'units': 2.0})

    def _runner(self, stop=None):
        @contextlib.contextmanager
        def env_factory():
            yield self.env
        return EtoroStreamRunner(env_factory, self.account.id, stop_event=stop or threading.Event())

    def test_protocol_messages(self):
        proto = EtoroStreamProtocol('API', 'USER', 'demo')
        auth = json.loads(proto.auth_message())
        self.assertEqual(auth['operation'], 'Authenticate')
        self.assertEqual(auth['data'], {'userKey': 'USER', 'apiKey': 'API'})
        sub = json.loads(proto.subscribe_message({'instrument:1001', 'private:demo'}))
        self.assertEqual(sub['operation'], 'Subscribe')
        self.assertEqual(sub['data'], {'topics': ['instrument:1001', 'private:demo'], 'snapshot': True})
        self.assertEqual(EtoroStreamProtocol('a', 'b', 'real').private_topic, 'private')
        parsed = proto.parse(_rate_msg('1001', 101.5, 102.5, 102.0))
        self.assertEqual(parsed['rates']['1001']['bid'], 101.5)
        self.assertEqual(parsed['rates']['1001']['last'], 102.0)
        private = proto.parse(json.dumps({'messages': [{'topic': 'private:demo', 'type': 'Trading.Position.Closed',
                                                        'content': '{"PositionID": 555}'}]}))
        self.assertEqual(private['private'], ['Trading.Position.Closed'])
        self.assertEqual(proto.parse('no es json'), {'rates': {}, 'private': [], 'ack': None})
        ok = proto.parse(json.dumps({'id': '1', 'success': True, 'operation': 'Authenticate'}))
        self.assertEqual(ok['ack'], ('Authenticate', True, ''))
        with self.assertRaises(StreamAuthError):
            proto.parse(json.dumps({'id': '1', 'success': False, 'operation': 'Authenticate', 'errorCode': 'InvalidKey'}))

    def test_topics(self):
        self.assertEqual(self.account._etoro_stream_topics(), {'instrument:1001', 'private:demo'})

    def test_session_applies_rates_and_syncs(self):
        stop = threading.Event()
        runner = self._runner(stop)
        ws = FakeWs([
            json.dumps({'id': '1', 'success': True, 'operation': 'Authenticate'}),
            _rate_msg('1001', 109.0, 111.0),
            json.dumps({'messages': [{'topic': 'private:demo', 'type': 'Trading.Position.Opened', 'content': '{}'}]}),
        ], stop)
        runner.connect = lambda: ws
        sent = []
        with patch.object(type(self.env['bus.bus']), '_sendone', lambda self, target, kind, msg: sent.append((kind, msg))), \
                patch.object(type(self.account), '_sync_portfolio', return_value='ok') as sync, \
                patch('odoo.addons.trading_etoro.stream.SYNC_DELAY_SECONDS', 0):
            runner.session(runner.load_config())
            if runner.sync_due:
                runner.sync_portfolio()
        self.assertEqual([m['operation'] for m in ws.sent], ['Authenticate', 'Subscribe'])
        self.assertEqual(set(ws.sent[1]['data']['topics']), {'instrument:1001', 'private:demo'})
        self.assertTrue(ws.closed)
        self.assertEqual((self.instrument.bid, self.instrument.ask, self.instrument.last_price), (109.0, 111.0, 110.0))
        self.assertAlmostEqual(self.instrument.change_pct, 10.0)
        self.assertTrue(self.instrument.price_checked_at)
        self.assertAlmostEqual(self.position.current_rate, 109.0)  # una compra se valora al bid
        self.assertEqual(self.account.stream_state, 'live')
        self.assertTrue(self.account.stream_live)
        self.assertIn('trading_prices', [k for k, _ in sent])
        self.assertIn('trading_positions', [k for k, _ in sent])
        self.assertEqual(sync.call_count, 1)

    def test_flush_keeps_rates_on_error(self):
        runner = self._runner()
        runner.pending_rates = {'1001': {'bid': 1.0, 'ask': 1.0}}
        with patch.object(type(self.instrument), '_apply_stream_rates', side_effect=RuntimeError('bloqueo')):
            runner.flush()
        self.assertIn('1001', runner.pending_rates)

    def test_cron_skips_fresh_prices(self):
        self.instrument.price_checked_at = fields.Datetime.now()
        with patch.object(type(self.instrument), '_refresh_prices', autospec=True) as refresh:
            self.env['trading.instrument']._cron_refresh_prices()
        called = refresh.call_args.args[0] if refresh.called else self.env['trading.instrument'].browse()
        self.assertNotIn(self.instrument, called)
