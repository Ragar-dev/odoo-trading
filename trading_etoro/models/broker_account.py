from odoo import fields, models


class TradingBrokerAccount(models.Model):
    _inherit = 'trading.broker.account'

    broker_type = fields.Selection(selection_add=[('etoro', 'eToro')], ondelete={'etoro': 'set default'})
    etoro_api_key = fields.Char('API key (x-api-key)', groups='trading_base.group_trading_manager', copy=False)
    etoro_user_key = fields.Char('User key (x-user-key)', groups='trading_base.group_trading_manager', copy=False)
    etoro_stream = fields.Boolean(
        'Precios en directo', default=True,
        help='Con el servicio «odoo etoro_stream» en marcha, los precios y los cambios de cartera llegan '
             'por WebSocket al momento. Sin él, Odoo sigue con las tareas programadas.')

    def _etoro_stream_topics(self):
        """Temas del WebSocket: instrumentos de las posiciones abiertas y del seguimiento, y avisos privados."""
        self.ensure_one()
        positions = self.env['trading.position'].search([('account_id', '=', self.id), ('state', '=', 'open')])
        instruments = positions.instrument_id | self.env['trading.instrument'].search(
            [('account_id', '=', self.id), ('watchlist', '=', True)])
        topics = {f'instrument:{ref}' for ref in instruments.mapped('broker_ref') if ref and ref.isdigit()}
        topics.add('private' if self.environment == 'real' else 'private:demo')
        return topics
