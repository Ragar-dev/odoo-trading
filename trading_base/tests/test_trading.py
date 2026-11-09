from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.trading_base.models.instrument import tradingview_symbol_for
from odoo.addons.trading_base.models.broker_adapter import ADAPTERS, BrokerAdapter, BrokerError, parse_datetime


class FakeAdapter(BrokerAdapter):
    portfolio = {'credit': 0.0, 'unrealized_pnl': 0.0, 'positions': []}
    rates = {}
    candles = []
    placed = []
    fail_orders = False

    def test_connection(self):
        return 'ok'

    def search_instruments(self, query):
        return [{'broker_ref': '1001', 'symbol': query.upper(), 'name': 'Apple', 'asset_type': 'stock'}]

    def get_instrument_info(self, broker_ref):
        return {'broker_ref': broker_ref, 'symbol': f'SYM{broker_ref}', 'name': 'Inst', 'asset_type': 'stock'}

    def get_rates(self, refs):
        return {r: v for r, v in self.rates.items() if r in refs}

    def get_candles(self, broker_ref, interval='OneDay', count=200):
        return self.candles

    def get_portfolio(self):
        return self.portfolio

    def place_order(self, order):
        if FakeAdapter.fail_orders:
            raise BrokerError('rechazada')
        FakeAdapter.placed.append(order.id)
        return {'broker_order_id': 'B-1', 'raw': {}}

    def close_position(self, position, units=None):
        return {}


@tagged('post_install', '-at_install')
class TestTrading(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.usd = cls.env.ref('base.USD')
        cls.account = cls.env['trading.broker.account'].create({
            'name': 'Demo test', 'broker_type': 'manual', 'currency_id': cls.usd.id,
        })
        cls.instrument = cls.env['trading.instrument'].create({
            'account_id': cls.account.id, 'broker_ref': '1001', 'symbol': 'AAPL', 'asset_type': 'stock',
        })

    def setUp(self):
        super().setUp()
        patcher = patch.dict(ADAPTERS, {'manual': FakeAdapter})
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeAdapter.portfolio = {'credit': 0.0, 'unrealized_pnl': 0.0, 'positions': []}
        FakeAdapter.rates = {}
        FakeAdapter.candles = []
        FakeAdapter.placed = []
        FakeAdapter.fail_orders = False

    def _position(self, **vals):
        return self.env['trading.position'].create({
            'account_id': self.account.id, 'instrument_id': self.instrument.id,
            'open_rate': 100.0, 'units': 2.0, 'amount': 200.0, **vals,
        })

    # ------------------------------------------------------------------
    def test_parse_datetime(self):
        self.assertEqual(parse_datetime('2026-11-05T10:20:30.1234567Z'), datetime(2026, 11, 5, 10, 20, 30, 123456))
        self.assertEqual(parse_datetime('2026-11-05T10:20:30+01:00'), datetime(2026, 11, 5, 9, 20, 30))
        self.assertFalse(parse_datetime(None))
        self.assertFalse(parse_datetime('basura'))

    def test_sync_portfolio_creates_and_closes_estimated(self):
        FakeAdapter.portfolio = {'credit': 500.0, 'unrealized_pnl': 12.5, 'positions': [{
            'broker_ref': 'P1', 'instrument_ref': '2002', 'side': 'buy', 'open_date': '2026-11-01T09:00:00Z',
            'open_rate': 50.0, 'amount': 100.0, 'units': 2.0, 'leverage': 1, 'fees': 0.0,
        }]}
        self.account._sync_portfolio()
        pos = self.env['trading.position'].search([('broker_ref', '=', 'P1')])
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos.instrument_id.symbol, 'SYM2002')
        self.assertEqual(self.account.equity, 612.5)
        self.assertEqual(self.account.state, 'ok')
        # Segunda sincronización: actualiza sin duplicar.
        self.account._sync_portfolio()
        self.assertEqual(self.env['trading.position'].search_count([('broker_ref', '=', 'P1')]), 1)
        # La posición desaparece del broker → cierre estimado con el último bid.
        pos.instrument_id.write({'bid': 60.0, 'ask': 60.2, 'last_price': 60.1})
        FakeAdapter.portfolio = {'credit': 620.0, 'unrealized_pnl': 0.0, 'positions': []}
        self.account._sync_portfolio()
        self.assertEqual(pos.state, 'closed')
        self.assertTrue(pos.close_estimated)
        self.assertAlmostEqual(pos.realized_pnl, 20.0)

    def test_sync_mirrors(self):
        FakeAdapter.portfolio = {'credit': 20.0, 'unrealized_pnl': 0.0, 'positions': [], 'mirrors': [{
            'broker_ref': 'M1', 'name': 'QuantumComputing', 'initial_investment': 500.0, 'available_amount': 10.0,
            'start_date': '2026-08-10T16:31:59.113Z',
            'positions': [{'broker_ref': 'C1', 'instrument_ref': '1001', 'side': 'buy', 'open_rate': 100.0,
                           'amount': 480.0, 'units': 4.8, 'leverage': 1}],
        }]}
        self.account._sync_portfolio()
        mirror = self.env['trading.mirror'].search([('account_id', '=', self.account.id)])
        self.assertEqual(mirror.name, 'QuantumComputing')
        pos = self.env['trading.position'].search([('broker_ref', '=', 'C1')])
        self.assertEqual(pos.mirror_id, mirror)
        self.instrument.write({'bid': 110.0, 'ask': 110.2, 'last_price': 110.1})
        self.assertAlmostEqual(mirror.unrealized_pnl, 48.0)          # 4,8 u × 10
        self.assertAlmostEqual(mirror.value, 10.0 + 480.0 + 48.0)
        self.assertAlmostEqual(mirror.total_pnl, 38.0)
        self.assertEqual(self.account.equity, 20.0 + 480.0 + 10.0)
        with self.assertRaises(UserError):
            pos.action_close_position()
        # La copia desaparece → se archiva y su posición se cierra estimada.
        FakeAdapter.portfolio = {'credit': 540.0, 'unrealized_pnl': 0.0, 'positions': [], 'mirrors': []}
        self.account._sync_portfolio()
        self.assertFalse(mirror.active)
        self.assertEqual(pos.state, 'closed')

    def test_environment_locked_with_positions(self):
        self.account.environment = 'real'   # sin posiciones: permitido
        self.account.environment = 'demo'
        self._position()
        with self.assertRaises(UserError):
            self.account.environment = 'real'

    def test_refresh_prices_tick_and_change(self):
        FakeAdapter.candles = [{'date': '2026-11-03T00:00:00Z', 'close': 90.0},
                               {'date': '2026-11-04T00:00:00Z', 'close': 95.0}]
        FakeAdapter.rates = {'1001': {'bid': 99.0, 'ask': 101.0, 'date': '2026-11-05T10:00:00Z', 'quote_type': 'realtime'}}
        self.instrument._refresh_prices()
        self.assertEqual(self.instrument.last_price, 100.0)
        self.assertEqual(self.instrument.prev_close, 90.0)
        self.assertAlmostEqual(self.instrument.change_pct, 11.11, places=2)
        self.assertEqual(self.instrument.tick_direction, 'flat')
        FakeAdapter.rates['1001'].update(bid=104.0, ask=106.0)
        self.instrument._refresh_prices()
        self.assertEqual(self.instrument.tick_direction, 'up')
        FakeAdapter.rates['1001'].update(bid=94.0, ask=96.0)
        self.instrument._refresh_prices()
        self.assertEqual(self.instrument.tick_direction, 'down')

    def test_refresh_prices_error_marks_account(self):
        with patch.object(FakeAdapter, 'get_rates', side_effect=BrokerError('caído')):
            self.instrument._refresh_prices()
            self.assertEqual(self.account.state, 'error')
            with self.assertRaises(BrokerError):
                self.instrument._refresh_prices(raise_errors=True)

    def test_unrealized_pnl_long_short(self):
        self.instrument.write({'bid': 110.0, 'ask': 111.0, 'last_price': 110.5})
        long_pos = self._position(side='buy')
        short_pos = self._position(side='sell')
        self.assertAlmostEqual(long_pos.unrealized_pnl, 20.0)     # 2 × (110 − 100)
        self.assertAlmostEqual(short_pos.unrealized_pnl, -22.0)   # 2 × (111 − 100) × −1
        self.assertAlmostEqual(long_pos.current_value, 220.0)     # 200 invertido + 20
        self.assertAlmostEqual(short_pos.current_value, 178.0)    # 200 invertido − 22
        closed = self._position(state='closed', close_rate=90.0, realized_pnl=-20.0)
        Position = self.env['trading.position']
        self.assertIn(long_pos, Position.search([('unrealized_pnl', '>', 0)]))
        self.assertNotIn(short_pos, Position.search([('unrealized_pnl', '>', 0)]))
        self.assertIn(short_pos, Position.search([('unrealized_pnl', '<', 0)]))
        self.assertNotIn(closed, Position.search([('unrealized_pnl', '<', 0)]))
        self.assertAlmostEqual(closed.current_value, 180.0)
        # Sin unidades: amount × leverage / open_rate.
        lev = self._position(side='buy', units=0.0, amount=100.0, leverage=2)
        self.assertAlmostEqual(lev.unrealized_pnl, 20.0)          # 2 u × 10

    def test_tax_class(self):
        self.assertEqual(self._position().tax_class, 'securities')
        self.assertEqual(self._position(leverage=5).tax_class, 'cfd')
        self.assertEqual(self._position(side='sell').tax_class, 'cfd')
        crypto = self.env['trading.instrument'].create({
            'account_id': self.account.id, 'broker_ref': '100000', 'symbol': 'BTC', 'asset_type': 'crypto'})
        self.assertEqual(self._position(instrument_id=crypto.id).tax_class, 'crypto')
        fx = self.env['trading.instrument'].create({
            'account_id': self.account.id, 'broker_ref': '1', 'symbol': 'EURUSD', 'asset_type': 'forex'})
        self.assertEqual(self._position(instrument_id=fx.id).tax_class, 'cfd')

    def test_order_real_blocked_demo_sent(self):
        order = self.env['trading.order'].create({
            'account_id': self.account.id, 'instrument_id': self.instrument.id, 'amount': 50.0})
        self.assertTrue(order.name.startswith('ORD/'))
        order.action_send()
        self.assertEqual(order.state, 'sent')
        self.assertEqual(order.broker_order_id, 'B-1')
        real = self.env['trading.broker.account'].create({
            'name': 'Real', 'environment': 'real', 'currency_id': self.usd.id})
        inst = self.env['trading.instrument'].create({'account_id': real.id, 'broker_ref': '1001', 'symbol': 'AAPL'})
        real_order = self.env['trading.order'].create({
            'account_id': real.id, 'instrument_id': inst.id, 'amount': 50.0})
        with self.assertRaises(UserError):
            real_order.action_send()
        self.assertEqual(FakeAdapter.placed, [order.id])
        real.allow_real_orders = True
        real_order.action_send()
        self.assertEqual(real_order.state, 'sent')

    def test_order_broker_error_keeps_state(self):
        FakeAdapter.fail_orders = True
        order = self.env['trading.order'].create({
            'account_id': self.account.id, 'instrument_id': self.instrument.id, 'amount': 50.0})
        result = order.action_send()
        self.assertEqual(order.state, 'error')
        self.assertEqual(order.error_message, 'rechazada')
        self.assertEqual(result['params']['type'], 'danger')
        order_zero = self.env['trading.order'].create({
            'account_id': self.account.id, 'instrument_id': self.instrument.id, 'amount': 0.0})
        with self.assertRaises(UserError):
            order_zero.action_send()

    def test_test_connection_error_sets_state(self):
        with patch.object(FakeAdapter, 'test_connection', side_effect=BrokerError('credenciales')):
            result = self.account.action_test_connection()
        self.assertEqual(self.account.state, 'error')
        self.assertEqual(self.account.last_error, 'credenciales')
        self.assertEqual(result['tag'], 'display_notification')

    def test_import_wizard(self):
        wizard = self.env['trading.instrument.import.wizard'].create({
            'account_id': self.account.id, 'query': 'nvda, aapl'})
        wizard.action_import()
        # '1001' ya existía (AAPL); el falso devuelve siempre 1001 → no duplica.
        self.assertEqual(self.env['trading.instrument'].search_count(
            [('account_id', '=', self.account.id), ('broker_ref', '=', '1001')]), 1)

    def test_import_wizard_no_exact_match(self):
        with patch.object(FakeAdapter, 'search_instruments', return_value=[
                {'broker_ref': '315', 'symbol': 'BTC.Fut', 'name': 'Futuro', 'asset_type': 'index'}]):
            wizard = self.env['trading.instrument.import.wizard'].create({
                'account_id': self.account.id, 'query': 'BTC'})
            result = wizard.action_import()
        self.assertFalse(self.env['trading.instrument'].search([('broker_ref', '=', '315')]))
        self.assertIn('BTC.Fut', result['params']['message'])
        self.assertEqual(result['params']['type'], 'warning')

    def test_load_candles(self):
        FakeAdapter.candles = [
            {'date': '2026-11-03T00:00:00Z', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 10},
            {'date': '2026-11-04T00:00:00Z', 'open': 1.5, 'high': 2.5, 'low': 1, 'close': 2, 'volume': 12},
        ]
        self.instrument.action_load_candles()
        self.instrument.action_load_candles()
        prices = self.env['trading.price'].search([('instrument_id', '=', self.instrument.id)])
        self.assertEqual(len(prices), 2)
        self.assertEqual(self.instrument.prev_close, 1.5)

    def test_live_board(self):
        FakeAdapter.rates = {'1001': {'bid': 10.0, 'ask': 10.2, 'date': '2026-11-05T10:00:00Z'}}
        self._position()
        data = self.env['trading.instrument'].get_live_board()
        self.assertEqual(data['refresh_seconds'], 5)
        symbols = [i['symbol'] for i in data['instruments']]
        self.assertIn('AAPL', symbols)
        self.assertEqual(self.instrument.last_price, 10.1)
        self.assertTrue(data['positions'])
        # Recién refrescado: no vuelve a pedir precios.
        with patch.object(FakeAdapter, 'get_rates', side_effect=AssertionError('no debería llamar')):
            self.env['trading.instrument'].get_live_board()

    def test_ecb_rates(self):
        company = self.env.company
        if company.currency_id.name != 'EUR':
            self.skipTest('La compañía no está en EUR')
        xml = b'''<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
<Cube><Cube time="2026-11-04"><Cube currency="USD" rate="1.0850"/><Cube currency="GBP" rate="0.8400"/></Cube></Cube>
</gesmes:Envelope>'''
        response = MagicMock(content=xml)
        with patch('odoo.addons.trading_base.models.res_currency.requests.get', return_value=response):
            self.assertTrue(self.env['res.currency']._cron_trading_ecb_rates())
            self.env['res.currency']._cron_trading_ecb_rates()  # idempotente
        rates = self.env['res.currency.rate'].search([
            ('currency_id', '=', self.usd.id), ('company_id', '=', company.id), ('name', '=', '2026-11-04')])
        self.assertEqual(len(rates), 1)
        self.assertAlmostEqual(rates.rate, 1.085)

    def test_account_move(self):
        acc = self.account
        if not (acc.journal_id and acc.broker_asset_account_id and acc.gain_account_id and acc.loss_account_id):
            self.skipTest('Sin plan contable cargado')
        self.assertTrue(acc.gain_account_id.code.startswith('766'))
        self.assertTrue(acc.loss_account_id.code.startswith('666'))
        self.assertTrue(acc.broker_asset_account_id.code.startswith('572'))
        when = fields.Datetime.now() - timedelta(days=1)
        rate_vals = {'currency_id': self.usd.id, 'company_id': self.env.company.id, 'name': when.date()}
        rate = self.env['res.currency.rate'].search([(k, '=', v) for k, v in rate_vals.items()])
        rate_value = 1.25 if self.env.company.currency_id.name == 'EUR' else 1.0
        if rate:
            rate.rate = rate_value
        else:
            self.env['res.currency.rate'].create({**rate_vals, 'rate': rate_value})
        gain = self._position(state='closed', close_date=when, close_rate=110.0, realized_pnl=25.0)
        loss = self._position(state='closed', close_date=when, close_rate=90.0, realized_pnl=-12.5)
        (gain | loss).action_create_move()
        self.assertEqual(gain.move_id.state, 'posted')
        if self.env.company.currency_id.name == 'EUR':
            self.assertAlmostEqual(gain.realized_pnl_company, 20.0)
        g_credit = gain.move_id.line_ids.filtered(lambda l: l.credit)
        self.assertEqual(g_credit.account_id, acc.gain_account_id)
        l_debit = loss.move_id.line_ids.filtered(lambda l: l.debit)
        self.assertEqual(l_debit.account_id, acc.loss_account_id)
        with self.assertRaises(UserError):
            self._position().action_create_move()

    def test_tradingview_symbol(self):
        cases = [
            ('MSFT', 'stock', 'MSFT'), ('IFX.DE', 'stock', 'XETR:IFX'), ('QNT.US', 'stock', 'QNT'),
            ('TTWO.RTH', 'stock', 'TTWO'), ('BARC.L', 'stock', 'LSE:BARC'), ('BTC', 'crypto', 'BINANCE:BTCUSDT'),
            ('EURUSD', 'forex', 'FX:EURUSD'), ('SPX500', 'index', 'SP:SPX'), ('GOLD', 'commodity', 'TVC:GOLD'),
            ('NASDAQ:AAPL', 'stock', 'NASDAQ:AAPL'), ('', 'stock', False),
        ]
        for symbol, asset, expected in cases:
            self.assertEqual(tradingview_symbol_for(symbol, asset), expected, symbol)
        pos = self._position()
        self.assertTrue(pos.tradingview_symbol)
        pos.instrument_id.tradingview_symbol = 'NASDAQ:AAPL'
        self.assertEqual(pos.tradingview_symbol, 'NASDAQ:AAPL')

    def test_chart_data(self):
        FakeAdapter.candles = [
            {'date': '2026-11-03T00:00:00Z', 'open': 10.0, 'high': 12.0, 'low': 9.0, 'close': 11.0, 'volume': 5.0},
            {'date': '2026-11-04T00:00:00Z', 'open': 11.0, 'high': 13.0, 'low': 10.0, 'close': 12.0, 'volume': 6.0},
            {'date': '2026-11-04T00:00:00Z', 'open': 11.0, 'high': 13.0, 'low': 10.0, 'close': 12.0, 'volume': 6.0},
        ]
        pos = self._position(open_date=datetime(2026, 11, 3, 15, 0), stop_loss=0.0001, take_profit=130.0)
        with patch.object(FakeAdapter, 'get_candles', autospec=True, side_effect=lambda *a, **k: FakeAdapter.candles) as fetch:
            data = self.instrument.get_chart_data('OneDay', pos.id)
            self.assertEqual(fetch.call_count, 1)
            # Velas viejas pero recién consultadas: no se vuelve a pedir al broker.
            self.instrument.get_chart_data('OneDay')
            self.assertEqual(fetch.call_count, 1)
            # Intervalo no admitido -> diario.
            self.assertEqual(self.instrument.get_chart_data('Raro')['interval'], 'OneDay')
        self.assertEqual([c['time'] for c in data['candles']], [1793664000, 1793750400])
        self.assertEqual(data['step'], 86400)
        mark = data['positions'][0]
        self.assertTrue(mark['focus'])
        self.assertEqual(mark['stop_loss'], 0.0)  # 0,0001 = sin stop en eToro
        self.assertEqual(mark['take_profit'], 130.0)
        self.assertEqual(mark['open_time'], 1793718000)

    def test_chart_data_broker_error(self):
        with patch.object(FakeAdapter, 'get_candles', side_effect=BrokerError('caído')):
            data = self.instrument.get_chart_data('OneHour')
        self.assertEqual(data['candles'], [])
        self.assertIn('caído', data['error'])

    def test_chart_drawings(self):
        Drawing = self.env['trading.chart.drawing']
        line = Drawing.chart_create({'instrument_id': self.instrument.id, 'kind': 'trend', 'time1': 100,
                                     'price1': 1.5, 'time2': 200, 'price2': 2.5, 'color': '#ef5350',
                                     'company_id': 999})
        self.assertEqual(line['kind'], 'trend')
        self.assertEqual(line['color'], '#ef5350')
        Drawing.chart_create({'instrument_id': self.instrument.id, 'kind': 'hline', 'price1': 3.0})
        data = self.instrument.get_chart_data('OneDay')
        self.assertEqual(len(data['drawings']), 2)
        self.assertEqual(Drawing.browse(line['id']).company_id, self.instrument.company_id)

    def test_chart_prefs(self):
        Drawing = self.env['trading.chart.drawing']
        self.assertEqual(Drawing.chart_prefs()['buy_color'], '#26a69a')
        saved = Drawing.save_chart_prefs({'buy_color': '#FF9800', 'show_markers': False, 'marker_size': 9,
                                          'sell_color': 'red; x', 'otra': 1})
        self.assertEqual(saved['buy_color'], '#ff9800')
        self.assertFalse(saved['show_markers'])
        self.assertEqual(saved['marker_size'], 3)
        self.assertEqual(saved['sell_color'], '#ef5350')
        self.assertNotIn('otra', saved)
        # Un cambio parcial conserva lo anterior.
        again = Drawing.save_chart_prefs({'marker_size': 2})
        self.assertEqual((again['buy_color'], again['marker_size']), ('#ff9800', 2))
        self.assertEqual(self.instrument.get_chart_data('OneDay')['prefs'], again)
