import logging
import xml.etree.ElementTree as ET

import requests

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

ECB_URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml'
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
