import base64
import io
from datetime import datetime

import openpyxl

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.trading_etoro.wizards.statement_import import _date, _num


def _xlsx(rows, sheet='Closed Positions', header=None):
    book = openpyxl.Workbook()
    book.active.title = 'Account Summary'
    ws = book.create_sheet(sheet)
    ws.append(['Closed Positions report'])
    ws.append(header or ['Position ID', 'Action', 'Long / Short', 'Amount', 'Units / Contracts', 'Open Date',
                         'Close Date', 'Leverage', 'Spread Fees (USD)', 'Profit(USD)', 'Profit(EUR)',
                         'Open Rate', 'Close Rate', 'Overnight Fees and Dividends', 'Copied From'])
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    book.save(buf)
    return base64.b64encode(buf.getvalue())


@tagged('post_install', '-at_install')
class TestEtoroStatement(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.usd = cls.env.ref('base.USD')
        cls.account = cls.env['trading.broker.account'].create({
            'name': 'eToro test', 'broker_type': 'etoro', 'environment': 'demo', 'currency_id': cls.usd.id,
        })
        cls.eurusd = cls.env['trading.instrument'].create({
            'account_id': cls.account.id, 'broker_ref': '1', 'symbol': 'EURUSD', 'name': 'EUR/USD',
            'asset_type': 'forex',
        })
        cls.estimated = cls.env['trading.position'].create({
            'account_id': cls.account.id, 'instrument_id': cls.eurusd.id, 'broker_ref': '3629966664',
            'side': 'sell', 'open_date': datetime(2026, 11, 6, 13, 40), 'open_rate': 1.11538,
            'amount': 50.0, 'leverage': 30, 'state': 'closed', 'close_date': datetime(2026, 11, 6, 13, 59),
            'close_rate': 1.11465, 'realized_pnl': 0.98, 'close_estimated': True,
        })

    def _import(self, data):
        wizard = self.env['trading.etoro.statement.import'].create({'account_id': self.account.id, 'file': data})
        wizard.action_import()
        return wizard

    def test_helpers(self):
        self.assertEqual(_num('1.234,56'), 1234.56)
        self.assertEqual(_num('1,234.56'), 1234.56)
        self.assertEqual(_num('(0,87)'), -0.87)
        self.assertEqual(_num('-0.12 $'), -0.12)
        self.assertEqual(_date('06/11/2026 13:59:37'), datetime(2026, 11, 6, 13, 59, 37))

    def test_import_updates_creates_and_skips(self):
        data = _xlsx([
            # Posición estimada en Odoo -> se corrige con el cierre real.
            ['3629966664', 'Sell EUR/USD', 'Short', 50, 1344.5, '06/11/2026 13:40:14', '06/11/2026 13:59:37',
             30, 0.07, '0,91', 0.81, 1.11538, 1.11470, -0.02, None],
            # No estaba en Odoo -> se crea.
            ['3630000001', 'Buy EUR/USD', 'Long', 25.0, 670.1, '07/11/2026 09:00:00', '07/11/2026 10:30:00',
             30, 0.04, -0.33, -0.29, 1.1150, 1.1145, 0, None],
            # Instrumento desconocido -> se omite.
            ['3630000002', 'Buy Apple', 'Long', 100, 0.5, '07/11/2026 15:30:00', '07/11/2026 18:00:00',
             1, 0, 2.5, 2.2, 200, 205, 0, None],
        ])
        wizard = self._import(data)
        self.assertEqual(wizard.state, 'done')
        self.assertIn('Actualizadas con el cierre real: 1', wizard.result)
        self.assertIn('Posiciones cerradas leídas: 3', wizard.result)
        self.assertIn('Creadas (no estaban en Odoo): 1', wizard.result)
        self.assertIn('Apple', wizard.result)

        pos = self.estimated
        self.assertTrue(pos.broker_confirmed)
        self.assertFalse(pos.close_estimated)
        self.assertAlmostEqual(pos.close_rate, 1.11470)
        self.assertAlmostEqual(pos.realized_pnl, 0.89)  # 0,91 de beneficio - 0,02 de comisión nocturna
        self.assertAlmostEqual(pos.fees, 0.09)
        self.assertEqual(pos.close_date, datetime(2026, 11, 6, 13, 59, 37))

        new = self.env['trading.position'].search([('broker_ref', '=', '3630000001')])
        self.assertEqual((new.side, new.state, new.leverage), ('buy', 'closed', 30))
        self.assertAlmostEqual(new.realized_pnl, -0.33)
        self.assertEqual(new.tax_class, 'cfd')

        # Reimportar el mismo extracto no cambia nada.
        again = self._import(data)
        self.assertIn('Ya confirmadas, sin cambios: 2', again.result)

    def test_spanish_headers(self):
        data = _xlsx([['3629966664', 'Vender EUR/USD', 'Corto', 50, 1, '06/11/2026 13:40:14',
                       '06/11/2026 13:59:37', 30, 0.07, 1.10, 1.11538, 1.11470]],
                     sheet='Posiciones cerradas',
                     header=['ID de posición', 'Acción', 'Largo / Corto', 'Importe', 'Unidades', 'Fecha de apertura',
                             'Fecha de cierre', 'Apalancamiento', 'Spread', 'Beneficio (USD)', 'Tasa de apertura',
                             'Tasa de cierre'])
        self._import(data)
        self.assertAlmostEqual(self.estimated.realized_pnl, 1.10)

    def test_bad_files(self):
        with self.assertRaises(UserError):
            self._import(base64.b64encode(b'no es un excel'))
        with self.assertRaises(UserError):
            self._import(_xlsx([], sheet='Otra cosa'))

    def test_dividends_and_interest(self):
        book = openpyxl.Workbook()
        book.active.title = 'Account Summary'
        div = book.create_sheet('Dividends')
        div.append(['Date of Payment', 'Instrument Name', 'Net Dividend Received (USD)', 'Net Dividend Received (EUR)',
                    'Withholding Tax Rate (%)', 'Withholding Tax Amount (USD)', 'Withholding Tax Amount (EUR)',
                    'Position ID', 'Type', 'ISIN'])
        div.append(['15/03/2026', 'Cameco Corp', 0.85, 0.78, '15 %', 0.15, 0.14, '3629966664', 'Stocks', 'CA13321L1085'])
        div.append(['16/03/2026', 'EUR/USD', 0, 0, 0, 0, 0, '1', 'CFD', ''])
        act = book.create_sheet('Account Activity')
        act.append(['Date', 'Type', 'Details', 'Amount', 'Units', 'Position ID'])
        act.append(['01/04/2026 08:00:00', 'Interest Payment', 'Interés del saldo', 1.25, '', ''])
        act.append(['01/04/2026 09:00:00', 'Deposit', 'Ingreso', 500, '', ''])
        buf = io.BytesIO()
        book.save(buf)
        data = base64.b64encode(buf.getvalue())
        wizard = self._import(data)
        self.assertIn('Dividendos nuevos: 1', wizard.result)
        self.assertIn('Intereses nuevos: 1', wizard.result)
        Income = self.env['trading.income']
        dividend = Income.search([('account_id', '=', self.account.id), ('kind', '=', 'dividend')])
        self.assertAlmostEqual(dividend.gross, 1.0)
        self.assertAlmostEqual(dividend.withholding, 0.15)
        self.assertEqual(dividend.country_code, 'CA')
        self.assertEqual(dividend.instrument_id, self.eurusd)  # por la posición 3629966664
        interest = Income.search([('account_id', '=', self.account.id), ('kind', '=', 'interest')])
        self.assertAlmostEqual(interest.gross, 1.25)
        again = self._import(data)
        self.assertIn('ya importados: 1', again.result)
