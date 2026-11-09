import uuid
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.trading_base.models.broker_adapter import BrokerError

REQUEST = 'odoo.addons.trading_etoro.models.etoro_adapter.requests.request'


def _resp(payload=None, status=200, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload if payload is not None else {}
    r.content = b'{}' if payload is not None else b''
    r.text = ''
    return r


@tagged('post_install', '-at_install')
class TestEtoro(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env['trading.broker.account'].create({
            'name': 'eToro demo', 'broker_type': 'etoro', 'environment': 'demo',
            'etoro_api_key': 'API', 'etoro_user_key': 'USER',
        })
        cls.instrument = cls.env['trading.instrument'].create({
            'account_id': cls.account.id, 'broker_ref': '1001', 'symbol': 'AAPL', 'asset_type': 'stock'})
        cls.adapter = cls.account._get_adapter()

    def test_headers_and_missing_keys(self):
        with patch(REQUEST, return_value=_resp({'results': []})) as req:
            self.adapter.get_rates(['1001'])
        headers = req.call_args.kwargs['headers']
        self.assertEqual(headers['x-api-key'], 'API')
        self.assertEqual(headers['x-user-key'], 'USER')
        uuid.UUID(headers['x-request-id'])
        self.assertNotIn('Authorization', headers)
        bare = self.env['trading.broker.account'].create({'name': 'x', 'broker_type': 'etoro'})
        with self.assertRaises(BrokerError):
            bare._get_adapter().get_rates(['1'])

    def test_search_and_info(self):
        payload = {'items': [
            {'instrumentId': 8754, 'internalSymbolFull': 'AAPL.RTH', 'displayname': 'Apple', 'internalAssetClassId': 5},
            {'instrumentId': 1001, 'internalSymbolFull': 'AAPL', 'displayname': 'Apple', 'internalAssetClassId': 5},
        ]}
        with patch(REQUEST, return_value=_resp(payload)) as req:
            res = self.adapter.search_instruments('aapl')
        self.assertEqual(res[0], {'broker_ref': '1001', 'symbol': 'AAPL', 'name': 'Apple', 'asset_type': 'stock'})
        self.assertEqual(req.call_args.kwargs['params']['pageSize'], 100)
        args, kwargs = req.call_args
        self.assertEqual(args, ('GET', 'https://public-api.etoro.com/api/v1/market-data/search'))
        self.assertEqual(kwargs['params']['internalSymbolFull'], 'AAPL')
        self.assertIn('instrumentId', kwargs['params']['fields'])
        with patch(REQUEST, return_value=_resp({'items': [{'instrumentId': 100000, 'internalSymbolFull': 'BTC', 'internalAssetClassId': 10}]})) as req:
            info = self.adapter.get_instrument_info('100000')
        self.assertEqual(info['asset_type'], 'crypto')
        self.assertEqual(req.call_args.kwargs['params']['instrumentId'], '100000')

    def test_rates(self):
        payload = {'results': [
            {'instrumentId': 1001, 'bid': 180.1, 'ask': 180.3, 'date': '2026-11-05T10:00:00.1234567Z', 'quoteType': 'delayed'},
            {'instrumentId': 1002, 'bid': 1, 'ask': 2, 'quoteType': 'realtime'},
        ]}
        with patch(REQUEST, return_value=_resp(payload, status=206)) as req:
            rates = self.adapter.get_rates(['1001', '1002'])
        self.assertEqual(req.call_args.args[1], 'https://public-api.etoro.com/api/v2/market-data/rates')
        self.assertEqual(req.call_args.kwargs['params'], {'instrumentIds': '1001,1002'})
        self.assertEqual(rates['1001']['bid'], 180.1)
        self.assertEqual(rates['1001']['quote_type'], 'delayed')
        self.assertEqual(rates['1002']['quote_type'], 'realtime')

    def test_candles(self):
        payload = {'interval': 'OneDay', 'candles': [{'instrumentId': 1001, 'candles': [
            {'fromDate': '2026-11-04T00:00:00Z', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 9},
            {'fromDate': '2026-11-03T00:00:00Z', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.2, 'volume': 9},
        ]}]}
        with patch(REQUEST, return_value=_resp(payload)) as req:
            candles = self.adapter.get_candles('1001', 'OneDay', 5000)
        self.assertTrue(req.call_args.args[1].endswith('/instruments/1001/history/candles/asc/OneDay/1000'))
        self.assertEqual([c['close'] for c in candles], [1.2, 1.5])

    def test_portfolio_demo_and_real(self):
        payload = {'clientPortfolio': {'credit': 9000.5, 'unrealizedPnL': 25.0, 'positions': [{
            'positionId': 555, 'instrumentId': 1001, 'isBuy': False, 'openRate': 180.0, 'amount': 100.0,
            'units': 0.55, 'leverage': 2, 'stopLossRate': 200.0, 'takeProfitRate': 150.0,
            'openDateTime': '2026-11-01T09:00:00Z', 'totalFees': 0.3, 'unrealizedPnL': {'pnL': 3.2},
        }], 'mirrors': [{
            'mirrorID': 11590794, 'parentUsername': 'QuantumComputing', 'isPaused': False,
            'initialInvestment': 500.0, 'availableAmount': 0.32, 'stopLossAmount': 25.0, 'stopLossPercentage': 5.0,
            'startedCopyDate': '2026-08-10T16:31:59.113Z', 'closedPositionsNetProfit': 0.0,
            'depositSummary': 0.0, 'withdrawalSummary': 0.0,
            'positions': [{'positionID': 777, 'instrumentID': 1001, 'isBuy': True, 'openRate': 300.0,
                           'amount': 20.0, 'units': 0.0666, 'leverage': 1, 'mirrorID': 11590794,
                           'openDateTime': '2026-08-10T16:32:00Z', 'unrealizedPnL': {'pnL': 1.5}}],
        }]}}
        with patch(REQUEST, return_value=_resp(payload)) as req:
            data = self.adapter.get_portfolio()
        mirror = data['mirrors'][0]
        self.assertEqual((mirror['broker_ref'], mirror['name'], mirror['initial_investment']), ('11590794', 'QuantumComputing', 500.0))
        self.assertEqual(mirror['positions'][0]['broker_ref'], '777')
        self.assertTrue(req.call_args.args[1].endswith('/api/v1/trading/info/demo/pnl'))
        self.assertEqual(data['credit'], 9000.5)
        pos = data['positions'][0]
        self.assertEqual((pos['broker_ref'], pos['instrument_ref'], pos['side'], pos['leverage']), ('555', '1001', 'sell', 2))
        self.assertEqual(pos['broker_pnl'], 3.2)
        # Volcado completo en Odoo.
        with patch(REQUEST, return_value=_resp(payload)):
            self.account._sync_portfolio()
        position = self.env['trading.position'].search([('broker_ref', '=', '555')])
        self.assertEqual(position.instrument_id, self.instrument)
        self.assertEqual(position.tax_class, 'cfd')
        self.assertFalse(position.mirror_id)
        copied = self.env['trading.position'].search([('broker_ref', '=', '777')])
        self.assertEqual(copied.mirror_id.name, 'QuantumComputing')
        self.assertEqual(copied.mirror_id.initial_investment, 500.0)
        self.assertEqual(self.account.mirror_count, 1)
        real = self.env['trading.broker.account'].create({
            'name': 'real', 'broker_type': 'etoro', 'environment': 'real',
            'etoro_api_key': 'A', 'etoro_user_key': 'U'})
        with patch(REQUEST, return_value=_resp(payload)) as req:
            real._get_adapter().get_portfolio()
        self.assertTrue(req.call_args.args[1].endswith('/api/v1/trading/info/real/pnl'))

    def test_place_order_body_and_routes(self):
        order = self.env['trading.order'].create({
            'account_id': self.account.id, 'instrument_id': self.instrument.id, 'side': 'buy',
            'amount': 50.0, 'leverage': 1, 'stop_loss': 150.0})
        with patch(REQUEST, return_value=_resp({'token': 't', 'orderId': 987, 'referenceId': 'r'})) as req:
            result = self.adapter.place_order(order)
        self.assertEqual(result['broker_order_id'], 987)
        args, kwargs = req.call_args
        self.assertEqual(args, ('POST', 'https://public-api.etoro.com/api/v2/trading/execution/demo/orders'))
        body = kwargs['json']
        self.assertEqual(body['transaction'], 'buy')
        self.assertEqual(body['instrumentId'], 1001)
        self.assertEqual(body['amount'], 50.0)
        self.assertNotIn('units', body)
        self.assertEqual(body['stopLossType'], 'fixed')
        self.assertNotIn('takeProfitRate', body)
        order.write({'size_mode': 'units', 'units': 3})
        real = self.env['trading.broker.account'].create({
            'name': 'real', 'broker_type': 'etoro', 'environment': 'real',
            'etoro_api_key': 'A', 'etoro_user_key': 'U'})
        with patch(REQUEST, return_value=_resp({'orderId': 1})) as req:
            real._get_adapter().place_order(order)
        self.assertEqual(req.call_args.args[1], 'https://public-api.etoro.com/api/v2/trading/execution/orders')
        self.assertEqual(req.call_args.kwargs['json']['units'], 3)
        self.assertNotIn('amount', req.call_args.kwargs['json'])

    def test_close_position(self):
        pos = self.env['trading.position'].create({
            'account_id': self.account.id, 'instrument_id': self.instrument.id, 'broker_ref': '555',
            'open_rate': 100.0, 'units': 1.0})
        with patch(REQUEST, return_value=_resp({})) as req:
            self.adapter.close_position(pos)
        self.assertTrue(req.call_args.args[1].endswith('/api/v1/trading/execution/demo/market-close-orders/positions/555'))
        self.assertEqual(req.call_args.kwargs['json'], {'InstrumentId': 1001, 'UnitsToDeduct': None})

    def test_errors(self):
        with patch(REQUEST, return_value=_resp({}, status=429, headers={'Retry-After': '30'})):
            with self.assertRaisesRegex(BrokerError, '30 s'):
                self.adapter.get_rates(['1001'])
        with patch(REQUEST, return_value=_resp({}, status=401)):
            with self.assertRaisesRegex(BrokerError, 'credenciales'):
                self.adapter.get_portfolio()
        with patch(REQUEST, return_value=_resp({'message': 'Invalid instrument'}, status=422)):
            with self.assertRaisesRegex(BrokerError, 'Invalid instrument'):
                self.adapter.get_rates(['1'])
        result = None
        with patch(REQUEST, return_value=_resp({}, status=401)):
            result = self.account.action_test_connection()
        self.assertEqual(self.account.state, 'error')
        self.assertEqual(result['params']['type'], 'danger')
