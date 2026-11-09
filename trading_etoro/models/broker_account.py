from odoo import fields, models


class TradingBrokerAccount(models.Model):
    _inherit = 'trading.broker.account'

    broker_type = fields.Selection(selection_add=[('etoro', 'eToro')], ondelete={'etoro': 'set default'})
    etoro_api_key = fields.Char('API key (x-api-key)', groups='trading_base.group_trading_manager', copy=False)
    etoro_user_key = fields.Char('User key (x-user-key)', groups='trading_base.group_trading_manager', copy=False)
