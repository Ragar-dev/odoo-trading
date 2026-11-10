"""Informe para la renta (IRPF) de las operaciones de trading.

Reglas que aplica (orientativas, a revisar por el asesor):
- Acciones, ETF y criptoactivos: valor de adquisición y de transmisión en euros, cada uno al
  cambio del BCE de su fecha, y método FIFO por valor homogéneo (las primeras compradas son
  las primeras vendidas), aunque el broker cierre otro lote.
- Regla de los dos meses (solo valores admitidos a negociación): una pérdida no se computa si
  se compraron valores homogéneos en los dos meses anteriores o posteriores a la venta.
- Derivados / CFD: resultado liquidado por el broker, convertido al cambio del día de cierre.
- Dividendos: bruto, retención en origen y deducción por doble imposición internacional,
  limitada al tipo del convenio (15 % por defecto).
"""
from collections import defaultdict
from datetime import datetime

import pytz
from dateutil.relativedelta import relativedelta

from odoo import api, fields, models

TREATY_RATE = 0.15
# Escala de la base del ahorro (estatal + autonómica). Vigente desde 2025 (Ley 7/2024).
SAVINGS_SCALE = [(6000.0, 0.19), (50000.0, 0.21), (200000.0, 0.23), (300000.0, 0.27), (float('inf'), 0.30)]
COMPENSATION_LIMIT = 0.25


def savings_tax(base):
    """Cuota íntegra de la base del ahorro según la escala."""
    tax, lower = 0.0, 0.0
    for upper, rate in SAVINGS_SCALE:
        if base <= lower:
            break
        tax += (min(base, upper) - lower) * rate
        lower = upper
    return tax


def marginal_rate(base):
    return next(rate for upper, rate in SAVINGS_SCALE if base < upper) if base >= 0 else SAVINGS_SCALE[0][1]


def after_tax(gains_net, capital_gross, foreign_withholding, deduction, other_savings=0.0):
    """Estimación del resultado neto tras el IRPF del ahorro.

    - Si el saldo de ganancias y pérdidas es negativo, compensa hasta el 25 % de los
      rendimientos del capital mobiliario; el resto queda para los 4 años siguientes.
    - El impuesto es el incremento de cuota que el trading añade sobre ``other_savings``.
    - La deducción por doble imposición no puede superar el impuesto de esas rentas.
    """
    compensation = 0.0
    carry_forward = 0.0
    if gains_net < 0:
        compensation = min(-gains_net, COMPENSATION_LIMIT * max(capital_gross, 0.0))
        carry_forward = -gains_net - compensation
    base = max(gains_net, 0.0) + max(capital_gross, 0.0) - compensation
    other = max(other_savings or 0.0, 0.0)
    tax_gross = savings_tax(other + base) - savings_tax(other)
    deduction_applied = min(deduction, tax_gross)
    tax = tax_gross - deduction_applied
    gross_result = gains_net + capital_gross
    net_result = gross_result - foreign_withholding - tax
    return {
        'gross_result': gross_result, 'foreign_withholding': foreign_withholding,
        'compensation': compensation, 'carry_forward': carry_forward, 'base': base,
        'other_savings': other, 'tax_gross': tax_gross, 'deduction': deduction_applied, 'tax': tax,
        'net_result': net_result,
        'effective_rate': 100.0 * (tax + foreign_withholding) / gross_result if gross_result > 0 else 0.0,
        'marginal_rate': 100.0 * marginal_rate(other + base),
    }
EPS = 1e-9
FOREIGN_ASSETS_LIMIT = 50000.0

SECTIONS = {
    'securities': 'Ganancias y pérdidas patrimoniales derivadas de la transmisión de acciones y '
                  'participaciones admitidas a negociación (acciones y ETF).',
    'crypto': 'Ganancias y pérdidas patrimoniales por transmisión de monedas virtuales (criptoactivos).',
    'cfd': 'Derivados y CFD: ganancias y pérdidas patrimoniales que no derivan de la transmisión de '
           'elementos patrimoniales (confírmelo con su asesor).',
    'dividend': 'Rendimientos del capital mobiliario: dividendos. La retención extranjera da derecho a la '
                'deducción por doble imposición internacional.',
    'interest': 'Rendimientos del capital mobiliario: intereses.',
}


class ReportTradingTax(models.AbstractModel):
    _name = 'report.trading_base.report_tax_year'
    _description = 'Informe para la renta (trading)'

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        year = int(data.get('year') or fields.Date.context_today(self).year)
        Account = self.env['trading.broker.account'].with_context(active_test=False)
        accounts = Account.browse(data.get('account_ids') or []).exists() or Account.search([])
        return self._tax_data(year, accounts, other_savings=float(data.get('other_savings') or 0.0))

    # ------------------------------------------------------------------
    @api.model
    def _tax_data(self, year, accounts, other_savings=0.0):
        company = accounts[:1].company_id or self.env.company
        ccy = company.currency_id
        tz = pytz.timezone(self.env.user.tz or 'Atlantic/Canary')

        def conv(amount, currency, when):
            if not amount:
                return 0.0
            day = when.date() if isinstance(when, datetime) else when
            return currency._convert(amount, ccy, company, day, round=False)

        def fmt_dt(value):
            if not value:
                return ''
            if isinstance(value, datetime):
                return pytz.utc.localize(value).astimezone(tz).strftime('%d/%m/%Y %H:%M')
            return value.strftime('%d/%m/%Y')

        Position = self.env['trading.position']
        closed = Position.search([('account_id', 'in', accounts.ids), ('state', '=', 'closed'),
                                  ('fiscal_year', '=', year)], order='close_date asc, id asc')
        fifo = self._fifo_rows(year, accounts, conv)
        securities = [r for r in fifo if r['tax_class'] == 'securities']
        crypto = [r for r in fifo if r['tax_class'] == 'crypto']
        cfd = [{
            'p': p, 'result': conv(p.realized_pnl, p.currency_id, p.close_date),
            'estimated': p.close_estimated and not p.broker_confirmed,
        } for p in closed.filtered(lambda p: p.tax_class == 'cfd')]

        incomes = self.env['trading.income'].search([('account_id', 'in', accounts.ids), ('fiscal_year', '=', year)],
                                                    order='date asc, id asc')
        dividends = []
        for inc in incomes.filtered(lambda i: i.kind == 'dividend'):
            gross = conv(inc.gross, inc.currency_id, inc.date)
            wh = conv(inc.withholding, inc.currency_id, inc.date)
            dividends.append({'i': inc, 'gross': gross, 'withholding': wh, 'net': gross - wh,
                              'deduction': min(wh, gross * TREATY_RATE)})
        interests = [{'i': inc, 'gross': conv(inc.gross, inc.currency_id, inc.date),
                      'withholding': conv(inc.withholding, inc.currency_id, inc.date)}
                     for inc in incomes.filtered(lambda i: i.kind == 'interest')]

        def block(rows, key='result'):
            values = [r[key] for r in rows]
            return {'count': len(rows), 'gains': sum(v for v in values if v > 0),
                    'losses': sum(v for v in values if v < 0), 'net': sum(values)}

        sec_block = block(securities, 'computable')
        sec_block.update({
            'transmission': sum(r['transmission'] for r in securities),
            'acquisition': sum(r['acquisition'] for r in securities),
            'deferred': sum(r['result'] - r['computable'] for r in securities),
        })
        crypto_block = block(crypto)
        crypto_block.update({'transmission': sum(r['transmission'] for r in crypto),
                             'acquisition': sum(r['acquisition'] for r in crypto)})
        div_block = {
            'count': len(dividends), 'gross': sum(d['gross'] for d in dividends),
            'withholding': sum(d['withholding'] for d in dividends),
            'deduction': sum(d['deduction'] for d in dividends),
        }
        int_block = {'count': len(interests), 'gross': sum(i['gross'] for i in interests),
                     'withholding': sum(i['withholding'] for i in interests)}
        gains_net = sec_block['net'] + crypto_block['net'] + block(cfd)['net']
        capital_net = div_block['gross'] + int_block['gross']
        net = after_tax(gains_net, capital_net, div_block['withholding'] + int_block['withholding'],
                        div_block['deduction'], other_savings)

        # Bienes en el extranjero (modelos 720 / 721): valores a la fecha de impresión.
        today = fields.Date.context_today(self)
        foreign = []
        for acc in accounts:
            open_pos = Position.search([('account_id', '=', acc.id), ('state', '=', 'open')])
            crypto_value = sum(p.current_value for p in open_pos if p.tax_class == 'crypto')
            securities_value = sum(p.current_value for p in open_pos if p.tax_class != 'crypto')
            foreign.append({
                'account': acc,
                'cash': conv(acc.credit, acc.currency_id, today),
                'securities': conv(securities_value, acc.currency_id, today),
                'crypto': conv(crypto_value, acc.currency_id, today),
            })
        foreign_total = {k: sum(f[k] for f in foreign) for k in ('cash', 'securities', 'crypto')}

        warnings = []
        estimated = closed.filtered(lambda p: p.close_estimated and not p.broker_confirmed)
        if estimated:
            warnings.append(f'{len(estimated)} operaciones tienen el cierre estimado (marcadas con *). Importe el '
                            'extracto oficial del broker para usar los cierres reales.')
        two_months = [r for r in securities if r['two_months']]
        if two_months:
            warnings.append(f'{len(two_months)} pérdidas no se computan este año por la regla de los dos meses '
                            '(marcadas con †): se integrarán cuando venda los valores homogéneos.')
        incomplete = [r for r in fifo if r['missing_units']]
        if incomplete:
            warnings.append(f'{len(incomplete)} ventas no tienen compras suficientes registradas para aplicar FIFO '
                            '(marcadas con ‡). Revise el valor de adquisición con el extracto del broker.')
        if any(v > FOREIGN_ASSETS_LIMIT for v in (foreign_total['cash'], foreign_total['securities'])):
            warnings.append('El saldo o los valores en el extranjero superan 50.000 €: compruebe la obligación del '
                            'modelo 720.')
        if foreign_total['crypto'] > FOREIGN_ASSETS_LIMIT:
            warnings.append('Los criptoactivos en el extranjero superan 50.000 €: compruebe el modelo 721.')

        return {
            'doc_ids': closed.ids, 'doc_model': 'trading.position', 'docs': closed,
            'year': year, 'company': company, 'currency': ccy, 'accounts': accounts,
            'sections': SECTIONS, 'treaty_rate': TREATY_RATE * 100,
            'securities': securities, 'sec_block': sec_block,
            'crypto': crypto, 'crypto_block': crypto_block,
            'cfd': cfd, 'cfd_block': block(cfd),
            'dividends': dividends, 'div_block': div_block,
            'interests': interests, 'int_block': int_block,
            'gains_net': gains_net, 'capital_net': capital_net, 'after_tax': net,
            'savings_scale': [(u, r * 100) for u, r in SAVINGS_SCALE],
            'foreign': foreign, 'foreign_total': foreign_total, 'today': fmt_dt(today),
            'warnings': warnings, 'fmt_dt': fmt_dt, 'tz_name': tz.zone,
            'printed_at': fmt_dt(fields.Datetime.now()),
            'estimated_ids': set(estimated.ids),
        }

    @api.model
    def _fifo_rows(self, year, accounts, conv):
        """Ventas del ejercicio de acciones, ETF y cripto valoradas con FIFO por valor homogéneo."""
        Position = self.env['trading.position']
        buys = Position.search([('account_id', 'in', accounts.ids), ('side', '=', 'buy'),
                                ('tax_class', 'in', ('securities', 'crypto'))], order='open_date asc, id asc')
        groups = defaultdict(list)
        for p in buys:
            groups[(p.instrument_id.symbol, p.tax_class)].append(p)
        rows = []
        for (_symbol, tax_class), positions in groups.items():
            lots = [{
                'pos': p, 'date': p.open_date, 'remaining': p._effective_units(),
                'unit_cost': conv(p.open_rate, p.currency_id, p.open_date),
            } for p in positions]
            sales = sorted((p for p in positions if p.state == 'closed' and p.close_date),
                           key=lambda p: (p.close_date, p.id))
            for sale in sales:
                units = sale._effective_units()
                need, cost, used = units, 0.0, []
                for lot in lots:
                    if need <= EPS:
                        break
                    if lot['remaining'] <= EPS or lot['date'] > sale.close_date:
                        continue
                    take = min(lot['remaining'], need)
                    lot['remaining'] -= take
                    need -= take
                    cost += take * lot['unit_cost']
                    used.append(lot)
                if sale.fiscal_year != year:
                    continue
                transmission = conv(units * sale.close_rate - (sale.fees or 0.0), sale.currency_id, sale.close_date)
                result = transmission - cost
                two_months = False
                if tax_class == 'securities' and result < 0:
                    low = sale.close_date - relativedelta(months=2)
                    high = sale.close_date + relativedelta(months=2)
                    two_months = any(
                        low <= lot['date'] <= high and (lot['remaining'] > EPS or lot['date'] > sale.close_date)
                        for lot in lots)
                rows.append({
                    'p': sale, 'tax_class': tax_class, 'units': units,
                    'acquisition_dates': sorted({lot['date'] for lot in used}),
                    'acquisition': cost, 'transmission': transmission, 'result': result,
                    'computable': 0.0 if two_months else result, 'two_months': two_months,
                    'missing_units': need > 1e-6,
                    'broker_result': conv(sale.realized_pnl, sale.currency_id, sale.close_date),
                    'estimated': sale.close_estimated and not sale.broker_confirmed,
                })
        return sorted(rows, key=lambda r: (r['p'].close_date, r['p'].id))
