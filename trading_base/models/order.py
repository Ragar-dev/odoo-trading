from odoo import api, fields, models
from odoo.exceptions import UserError

from .broker_adapter import BrokerError


class TradingOrder(models.Model):
    _name = 'trading.order'
    _description = 'Orden'
    _inherit = ['mail.thread']
    _order = 'id desc'
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True, copy=False, default='/')
    account_id = fields.Many2one('trading.broker.account', required=True, ondelete='restrict', tracking=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='account_id.currency_id')
    environment = fields.Selection(related='account_id.environment')
    instrument_id = fields.Many2one(
        'trading.instrument', required=True, ondelete='restrict', tracking=True,
        domain="[('account_id', '=', account_id)]")
    side = fields.Selection([('buy', 'Comprar'), ('sell', 'Vender')], required=True, default='buy', tracking=True)
    size_mode = fields.Selection([('amount', 'Importe'), ('units', 'Unidades')], required=True, default='amount')
    amount = fields.Monetary(currency_field='currency_id')
    units = fields.Float(digits=(16, 6))
    leverage = fields.Integer('Apalancamiento', default=1)
    stop_loss = fields.Float(digits=(16, 6))
    take_profit = fields.Float(digits=(16, 6))
    state = fields.Selection(
        [('draft', 'Borrador'), ('sent', 'Enviada'), ('error', 'Error'), ('cancel', 'Cancelada')],
        default='draft', required=True, tracking=True, copy=False)
    broker_order_id = fields.Char('ID de orden en el broker', readonly=True, copy=False)
    error_message = fields.Text(readonly=True, copy=False)
    sent_date = fields.Datetime(readonly=True, copy=False)

    _positive_leverage = models.Constraint('CHECK(leverage >= 1)', 'El apalancamiento mínimo es 1.')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('trading.order') or '/'
        return super().create(vals_list)

    def _validate_before_send(self):
        self.ensure_one()
        if self.state not in ('draft', 'error'):
            raise UserError(self.env._('Solo se envían órdenes en borrador o con error.'))
        if self.size_mode == 'amount' and self.amount <= 0:
            raise UserError(self.env._('Indique un importe positivo.'))
        if self.size_mode == 'units' and self.units <= 0:
            raise UserError(self.env._('Indique unas unidades positivas.'))
        self.account_id._check_can_trade()

    def action_send(self):
        self.ensure_one()
        self._validate_before_send()
        account = self.account_id
        try:
            result = account._get_adapter().place_order(self) or {}
        except BrokerError as e:
            self.write({'state': 'error', 'error_message': str(e)})
            self.message_post(body=self.env._('Error del broker: %s', e))
            return account._notify(str(e), 'danger', self.env._('Orden rechazada'))
        self.write({
            'state': 'sent', 'error_message': False, 'sent_date': fields.Datetime.now(),
            'broker_order_id': str(result.get('broker_order_id') or ''),
        })
        self.message_post(body=self.env._(
            'Orden enviada al broker (%(env)s). ID: %(id)s',
            env=account.environment, id=self.broker_order_id or '—'))
        try:
            account._sync_portfolio()
        except BrokerError:
            pass
        return account._notify(self.env._('Orden %s enviada.', self.name))

    def action_cancel(self):
        self.filtered(lambda o: o.state in ('draft', 'error')).write({'state': 'cancel'})

    def action_draft(self):
        self.filtered(lambda o: o.state in ('cancel', 'error')).write({'state': 'draft'})
