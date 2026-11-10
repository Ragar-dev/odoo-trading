import csv
import io
import logging
import zipfile
import xml.etree.ElementTree as ET
from datetime import timedelta

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

ECB_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
ECB_HIST_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip'
ECB_NS = {'e': 'http://www.ecb.int/vocabulary/2002-08-01/eurofxref'}


class ResCurrency(models.Model):
    _inherit = 'res.currency'

    @api.model
    def _trading_fetch_ecb(self):
        """→ (fecha, {código: unidades por 1 EUR})"""
        response = requests.get(ECB_URL, timeout=20)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        day = root.find('.//e:Cube[@time]', ECB_NS)
        if day is None:
            return False, {}
        rates = {c.get('currency'): float(c.get('rate')) for c in day.findall('e:Cube', ECB_NS)}
        return fields.Date.to_date(day.get('time')), rates

    @api.model
    def _cron_trading_ecb_rates(self):
        companies = self.env['res.company'].search([]).filtered(lambda c: c.currency_id.name == 'EUR')
        if not companies:
            return False
        try:
            date, rates = self._trading_fetch_ecb()
        except (requests.RequestException, ET.ParseError) as e:
            _logger.warning('Trading: no se pudieron descargar los tipos del BCE: %s', e)
            return False
        Rate = self.env['res.currency.rate']
        currencies = self.search([('name', 'in', list(rates))])
        for company in companies:
            for cur in currencies:
                existing = Rate.search([
                    ('currency_id', '=', cur.id), ('company_id', '=', company.id), ('name', '=', date)], limit=1)
                vals = {'rate': rates[cur.name]}
                if existing:
                    existing.write(vals)
                else:
                    Rate.create({**vals, 'currency_id': cur.id, 'company_id': company.id, 'name': date})
        return True

    @api.model
    def _trading_ensure_ecb_history(self, date_from):
        """Garantiza tipos del BCE desde ``date_from`` para las divisas de las cuentas de trading.
        Hacienda exige convertir cada compra y cada venta al cambio de su fecha.
        Devuelve False si no se pudieron descargar."""
        companies = self.env['res.company'].search([]).filtered(lambda c: c.currency_id.name == 'EUR')
        currencies = self.env['trading.broker.account'].with_context(active_test=False).search([]).currency_id
        currencies = currencies.filtered(lambda c: c.name != 'EUR')
        if not companies or not currencies or not date_from:
            return True
        Rate = self.env['res.currency.rate']
        missing = [(company, cur) for company in companies for cur in currencies
                   if not Rate.search_count([('currency_id', '=', cur.id), ('company_id', '=', company.id),
                                             ('name', '<=', date_from)], limit=1)]
        if not missing:
            return True
        try:
            response = requests.get(ECB_HIST_URL, timeout=60)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                text = archive.read(archive.namelist()[0]).decode('utf-8')
        except (requests.RequestException, zipfile.BadZipFile, KeyError, IndexError) as e:
            _logger.warning('Trading: no se pudo descargar el histórico del BCE: %s', e)
            return False
        reader = csv.reader(io.StringIO(text))
        header = [h.strip() for h in next(reader, [])]
        start = date_from - timedelta(days=10)
        rows = []
        for row in reader:
            if not row or not row[0].strip():
                continue
            day = fields.Date.to_date(row[0].strip())
            if day >= start:
                rows.append((day, row))
        vals = []
        for company, cur in missing:
            if cur.name not in header:
                continue
            col = header.index(cur.name)
            existing = set(Rate.search([('currency_id', '=', cur.id), ('company_id', '=', company.id),
                                        ('name', '>=', start)]).mapped('name'))
            for day, row in rows:
                value = row[col].strip() if col < len(row) else ''
                if day in existing or value in ('', 'N/A'):
                    continue
                vals.append({'currency_id': cur.id, 'company_id': company.id, 'name': day, 'rate': float(value)})
                existing.add(day)
        Rate.create(vals)
        _logger.info('Trading: %s tipos históricos del BCE cargados desde %s.', len(vals), start)
        return True
