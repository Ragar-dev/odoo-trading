"""Datos de la web interna de trading (/trading).

Todo se calcula con los permisos del usuario que consulta; el controlador solo deja pasar a los
usuarios del grupo Trading.
"""
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta

from odoo import api, fields, models

from .broker_adapter import BrokerError, parse_datetime

_logger = logging.getLogger(__name__)

HISTORY_DAYS = (7, 30, 90, 365)


class TradingDashboard(models.AbstractModel):
    _name = 'trading.dashboard'
    _description = 'Web interna de trading'

    # ------------------------------------------------------------------
    # Utilidades
    # ------------------------------------------------------------------
    def _to_company(self, amount, currency, company=None):
        company = company or self.env.company
        if not amount or not currency or currency == company.currency_id:
            return amount or 0.0
        return currency._convert(amount, company.currency_id, company, fields.Date.context_today(self), round=False)

    @staticmethod
    def _dt(value):
        return fields.Datetime.to_string(value) if value else ''

    def _day_pnl(self, position):
        """Resultado del día de una posición abierta: desde el cierre anterior o desde la apertura de hoy."""
        inst = position.instrument_id
        rate = position.current_rate
        if not rate:
            return 0.0
        today_start = datetime.combine(fields.Date.context_today(self), time.min)
        if position.open_date and position.open_date >= today_start or not inst.prev_close:
            return position.unrealized_pnl
        return position._pnl_at(rate) - position._pnl_at(inst.prev_close)

    # ------------------------------------------------------------------
    # Resumen (se consulta cada pocos segundos)
    # ------------------------------------------------------------------
    @api.model
    def get_summary(self):
        company = self.env.company
        ccy = company.currency_id
        Position = self.env['trading.position']
        accounts = self.env['trading.broker.account'].search([])
        open_pos = Position.search([('state', '=', 'open')], order='open_date desc')

        positions = []
        totals = defaultdict(float)
        allocation = defaultdict(float)
        for p in open_pos:
            day = self._day_pnl(p)
            value_c = self._to_company(p.current_value, p.currency_id, company)
            pnl_c = self._to_company(p.unrealized_pnl, p.currency_id, company)
            day_c = self._to_company(day, p.currency_id, company)
            invested_c = self._to_company(p.amount, p.currency_id, company)
            totals['value'] += value_c
            totals['unrealized'] += pnl_c
            totals['invested'] += invested_c
            totals['day'] += day_c
            allocation[p.asset_type or 'other'] += value_c
            positions.append({
                'id': p.id, 'symbol': p.instrument_id.symbol, 'name': p.instrument_id.name or '',
                'instrument_id': p.instrument_id.id, 'asset_type': p.asset_type or 'other',
                'side': p.side, 'units': p.units, 'amount': p.amount, 'leverage': p.leverage,
                'open_rate': p.open_rate, 'current_rate': p.current_rate, 'open_date': self._dt(p.open_date),
                'unrealized_pnl': p.unrealized_pnl, 'current_value': p.current_value,
                'pnl_pct': 100.0 * p.unrealized_pnl / p.amount if p.amount else 0.0,
                'day_pnl': day, 'change_pct': p.instrument_id.change_pct,
                'stop_loss': p.stop_loss, 'take_profit': p.take_profit,
                'tax_estimate': p.tax_estimate, 'net_after_tax': p.net_after_tax,
                'currency': p.currency_id.name or '', 'mirror': p.mirror_id.name or '',
                'mirror_id': p.mirror_id.id or False, 'account': p.account_id.name,
            })

        cash = sum(self._to_company(a.credit, a.currency_id, company) for a in accounts)
        equity = sum(self._to_company(a.equity, a.currency_id, company) for a in accounts)
        previous = totals['value'] - totals['day']

        mirrors = [{
            'id': m.id, 'name': m.name, 'paused': m.paused, 'start_date': self._dt(m.start_date),
            'invested': m.invested, 'value': m.value, 'unrealized_pnl': m.unrealized_pnl,
            'total_pnl': m.total_pnl, 'open_count': m.open_position_count, 'currency': m.currency_id.name or '',
            'pnl_pct': 100.0 * m.unrealized_pnl / m.invested if m.invested else 0.0,
        } for m in self.env['trading.mirror'].search([])]

        instruments = self.env['trading.instrument'].search([('watchlist', '=', True)])
        watchlist = [{
            'id': i.id, 'symbol': i.symbol, 'name': i.name or '', 'asset_type': i.asset_type,
            'last_price': i.last_price, 'bid': i.bid, 'ask': i.ask, 'change_pct': i.change_pct,
            'tick': i.tick_direction or 'flat', 'price_date': self._dt(i.price_date),
        } for i in instruments]

        activity = []
        recent = Position.search([], order='write_date desc', limit=60)
        for p in recent:
            activity.append({'kind': 'open', 'date': p.open_date, 'position': p})
            if p.state == 'closed' and p.close_date:
                activity.append({'kind': 'close', 'date': p.close_date, 'position': p})
        activity.sort(key=lambda a: a['date'] or datetime.min, reverse=True)
        activity = [{
            'kind': a['kind'], 'date': self._dt(a['date']), 'id': a['position'].id,
            'symbol': a['position'].instrument_id.symbol, 'name': a['position'].instrument_id.name or '',
            'side': a['position'].side, 'mirror': a['position'].mirror_id.name or '',
            'amount': a['position'].amount, 'currency': a['position'].currency_id.name or '',
            'pnl': a['position'].realized_pnl if a['kind'] == 'close' else 0.0,
            'estimated': a['position'].close_estimated if a['kind'] == 'close' else False,
        } for a in activity[:12]]

        return {
            'server_time': self._dt(fields.Datetime.now()),
            'currency': ccy.name, 'currency_symbol': ccy.symbol,
            'user': {'name': self.env.user.name},
            'live': any(accounts.mapped('stream_live')),
            'totals': {
                'equity': equity, 'cash': cash, 'value': totals['value'], 'invested': totals['invested'],
                'unrealized': totals['unrealized'],
                'unrealized_pct': 100.0 * totals['unrealized'] / totals['invested'] if totals['invested'] else 0.0,
                'day': totals['day'], 'day_pct': 100.0 * totals['day'] / previous if previous else 0.0,
                'open_count': len(open_pos), 'tax_open': sum(open_pos.mapped('tax_estimate')),
            },
            'accounts': [{
                'id': a.id, 'name': a.name, 'environment': a.environment, 'broker': a.broker_type,
                'currency': a.currency_id.name or '', 'equity': a.equity, 'credit': a.credit,
                'unrealized_pnl': sum(open_pos.filtered(lambda p, a=a: p.account_id == a).mapped('unrealized_pnl')),
                'open_count': len(open_pos.filtered(lambda p, a=a: p.account_id == a)),
                'state': a.state, 'stream_live': a.stream_live, 'last_sync': self._dt(a.last_sync),
            } for a in accounts],
            'positions': positions,
            'allocation': [{'type': k, 'value': v} for k, v in sorted(allocation.items(), key=lambda kv: -kv[1])],
            'mirrors': mirrors,
            'watchlist': watchlist,
            'activity': activity,
        }

    # ------------------------------------------------------------------
    # Evolución del resultado (al abrir la página o cambiar el rango)
    # ------------------------------------------------------------------
    # Instrumentos ya consultados en este proceso (id → (momento, desde)), para no repetir llamadas al broker.
    _candles_checked = {}

    def _ensure_daily_candles(self, instruments, date_from):
        """Velas diarias que falten para la gráfica de evolución.

        Solo crea velas (``trading.price``): no escribe en el instrumento, que el servicio en
        directo actualiza cada segundo (evita conflictos de escritura concurrente).
        """
        Price = self.env['trading.price'].sudo()
        now = fields.Datetime.now()
        stale_limit = now - timedelta(days=4)
        for inst in instruments.filtered(lambda i: i.account_id.broker_type != 'manual'):
            checked = self._candles_checked.get(inst.id)
            if checked and checked[0] > now - timedelta(hours=6) and checked[1] <= date_from:
                continue
            domain = [('instrument_id', '=', inst.id), ('interval', '=', 'OneDay')]
            first = Price.search(domain, order='date asc', limit=1)
            last = Price.search(domain, order='date desc', limit=1)
            if first and last and first.date <= date_from + timedelta(days=4) and last.date >= stale_limit:
                self._candles_checked[inst.id] = (now, date_from)
                continue
            count = min(max((now - date_from).days + 10, 30), 1000)
            try:
                candles = inst.sudo().account_id._get_adapter().get_candles(inst.broker_ref, 'OneDay', count) or []
            except BrokerError as e:
                _logger.info('Web de trading: sin velas diarias de %s: %s', inst.symbol, e)
                self._candles_checked[inst.id] = (now, date_from)
                continue
            existing = set(Price.search(domain).mapped('date'))
            to_create = []
            for c in candles:
                date = parse_datetime(c.get('date'))
                if date and date not in existing:
                    existing.add(date)
                    to_create.append({'instrument_id': inst.id, 'interval': 'OneDay', 'date': date,
                                      **{k: c.get(k) or 0.0 for k in ('open', 'high', 'low', 'close', 'volume')}})
            if to_create:
                Price.create(to_create)
            self._candles_checked[inst.id] = (now, date_from)

    @api.model
    def get_history(self, days=30):
        days = int(days) if int(days or 0) in HISTORY_DAYS else 30
        company = self.env.company
        today = fields.Date.context_today(self)
        start = today - timedelta(days=days)
        start_dt = datetime.combine(start, time.min)
        Position = self.env['trading.position']
        positions = Position.search([
            '|', ('state', '=', 'open'), ('close_date', '>=', start_dt)])
        positions = positions.filtered(lambda p: p.open_date and p.open_date.date() <= today)
        self._ensure_daily_candles(positions.instrument_id, start_dt)

        closes = defaultdict(dict)
        for price in self.env['trading.price'].search([
                ('instrument_id', 'in', positions.instrument_id.ids), ('interval', '=', 'OneDay'),
                ('date', '>=', start_dt - timedelta(days=7))], order='date asc'):
            closes[price.instrument_id.id][price.date.date()] = price.close

        points = []
        realized = 0.0
        last_close = {}
        day = start - timedelta(days=7)
        while day <= today:
            for inst_id, by_day in closes.items():
                if day in by_day:
                    last_close[inst_id] = by_day[day]
            if day >= start:
                unrealized = 0.0
                end_of_day = datetime.combine(day, time.max)
                for p in positions:
                    if p.open_date > end_of_day:
                        continue
                    if p.state == 'closed' and p.close_date and p.close_date <= end_of_day:
                        continue
                    if day == today and p.state == 'open':
                        pnl = p.unrealized_pnl
                    else:
                        rate = last_close.get(p.instrument_id.id)
                        pnl = p._pnl_at(rate) if rate else 0.0
                    unrealized += self._to_company(pnl, p.currency_id, company)
                realized = sum(
                    self._to_company(p.realized_pnl, p.currency_id, company) for p in positions
                    if p.state == 'closed' and p.close_date and start_dt <= p.close_date <= end_of_day)
                points.append({'time': fields.Date.to_string(day), 'value': round(unrealized + realized, 2),
                               'realized': round(realized, 2), 'unrealized': round(unrealized, 2)})
            day += timedelta(days=1)
        first = points[0]['value'] if points else 0.0
        last = points[-1]['value'] if points else 0.0
        return {'days': days, 'points': points, 'change': round(last - first, 2), 'currency': company.currency_id.name}

    # ------------------------------------------------------------------
    # Operaciones cerradas y renta
    # ------------------------------------------------------------------
    @api.model
    def get_closed(self, year=None):
        Position = self.env['trading.position']
        years = sorted({y for y in Position.search([('state', '=', 'closed')]).mapped('fiscal_year') if y}, reverse=True)
        year = int(year) if year else (years[0] if years else fields.Date.context_today(self).year)
        closed = Position.search([('state', '=', 'closed'), ('fiscal_year', '=', year)], order='close_date desc')
        rows = [{
            'id': p.id, 'symbol': p.instrument_id.symbol, 'name': p.instrument_id.name or '',
            'side': p.side, 'open_date': self._dt(p.open_date), 'close_date': self._dt(p.close_date),
            'open_rate': p.open_rate, 'close_rate': p.close_rate, 'amount': p.amount, 'leverage': p.leverage,
            'realized_pnl': p.realized_pnl, 'realized_pnl_company': p.realized_pnl_company,
            'tax_estimate': p.tax_estimate, 'net_after_tax': p.net_after_tax, 'currency': p.currency_id.name or '',
            'mirror': p.mirror_id.name or '', 'estimated': p.close_estimated, 'tax_class': p.tax_class,
        } for p in closed]
        wins = [r for r in rows if r['realized_pnl_company'] > 0]
        return {
            'year': year, 'years': years or [year], 'rows': rows,
            'totals': {
                'count': len(rows), 'wins': len(wins),
                'win_rate': 100.0 * len(wins) / len(rows) if rows else 0.0,
                'result': sum(r['realized_pnl_company'] for r in rows),
                'tax': sum(r['tax_estimate'] for r in rows),
                'net': sum(r['net_after_tax'] for r in rows),
            },
        }

    @api.model
    def get_tax(self, year=None):
        year = int(year) if year else fields.Date.context_today(self).year
        other = float(self.env['ir.config_parameter'].sudo().get_param('trading.other_savings', 0) or 0)
        accounts = self.env['trading.broker.account'].with_context(active_test=False).search([])
        data = self.env['report.trading_base.report_tax_year']._tax_data(year, accounts, other_savings=other)
        at = data['after_tax']
        return {
            'year': year, 'currency': data['currency'].name, 'other_savings': other,
            'blocks': {
                'securities': data['sec_block']['net'], 'crypto': data['crypto_block']['net'],
                'cfd': data['cfd_block']['net'], 'dividends': data['div_block']['gross'],
                'interest': data['int_block']['gross'],
            },
            'counts': {
                'securities': data['sec_block']['count'], 'crypto': data['crypto_block']['count'],
                'cfd': data['cfd_block']['count'], 'dividends': data['div_block']['count'],
                'interest': data['int_block']['count'],
            },
            'gross_result': at['gross_result'], 'base': at['base'], 'tax': at['tax'],
            'foreign_withholding': at['foreign_withholding'], 'deduction': at['deduction'],
            'net_result': at['net_result'], 'effective_rate': at['effective_rate'],
            'marginal_rate': at['marginal_rate'], 'carry_forward': at['carry_forward'],
            # El último tramo no tiene límite (inf no cabe en JSON): se envía como null.
            'scale': [(None if upto == float('inf') else upto, rate) for upto, rate in data['savings_scale']],
            'warnings': data['warnings'],
            'foreign_total': data['foreign_total'],
        }
