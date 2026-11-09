from odoo import fields, models
from odoo.exceptions import UserError

from ..models.broker_adapter import BrokerError


class TradingInstrumentImportWizard(models.TransientModel):
    _name = 'trading.instrument.import.wizard'
    _description = 'Importar instrumentos del broker'

    account_id = fields.Many2one('trading.broker.account', required=True)
    query = fields.Char('Símbolos', required=True, help='Uno o varios símbolos separados por comas, p. ej. AAPL, BTC, SPX500')

    def action_import(self):
        self.ensure_one()
        Instrument = self.env['trading.instrument']
        adapter = self.account_id._get_adapter()
        created = Instrument
        not_found = []
        ambiguous = []
        for term in [t.strip() for t in self.query.split(',') if t.strip()]:
            try:
                results = adapter.search_instruments(term) or []
            except BrokerError as e:
                raise UserError(str(e)) from e
            if not results:
                not_found.append(term)
                continue
            exact = [r for r in results if (r.get('symbol') or '').upper() == term.upper()]
            if not exact:
                # Sin coincidencia exacta no se importa nada «parecido»: se ofrecen candidatos.
                ambiguous.append(f"{term} → {', '.join(r.get('symbol') or '?' for r in results[:5])}")
                continue
            for r in exact:
                ref = str(r['broker_ref'])
                existing = Instrument.search([('account_id', '=', self.account_id.id), ('broker_ref', '=', ref)])
                if existing:
                    existing.watchlist = True
                    continue
                created |= Instrument.create({
                    'account_id': self.account_id.id, 'broker_ref': ref,
                    'symbol': r.get('symbol') or term.upper(), 'name': r.get('name') or r.get('symbol'),
                    'asset_type': r.get('asset_type') or 'other', 'watchlist': True,
                })
        if created:
            created._refresh_prices()
        message = self.env._('%s instrumentos importados.', len(created))
        if not_found:
            message += ' ' + self.env._('Sin resultados: %s', ', '.join(not_found))
        if ambiguous:
            message += ' ' + self.env._('Sin coincidencia exacta (¿quiso decir?): %s', '; '.join(ambiguous))
        kind = 'warning' if (not_found or ambiguous) else 'success'
        return self.env['trading.broker.account']._notify(message, kind)
