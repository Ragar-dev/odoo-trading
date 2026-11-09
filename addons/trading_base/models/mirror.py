from odoo import api, fields, models


class TradingMirror(models.Model):
    """Cartera copiada (CopyTrader o Smart Portfolio): agrupa posiciones que gestiona el broker."""
    _name = 'trading.mirror'
    _description = 'Cartera copiada'
    _order = 'account_id, name'
    _check_company_auto = True

    name = fields.Char('Cartera', required=True)
    broker_ref = fields.Char('ID en el broker', required=True, index=True)
    account_id = fields.Many2one('trading.broker.account', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='account_id.currency_id')
    active = fields.Boolean(default=True)
    paused = fields.Boolean('En pausa', readonly=True)
    start_date = fields.Datetime('Copiando desde', readonly=True)
    initial_investment = fields.Monetary('Inversión inicial', readonly=True)
    deposits = fields.Monetary('Aportaciones', readonly=True)
    withdrawals = fields.Monetary('Retiradas', readonly=True)
    available_amount = fields.Monetary('Efectivo disponible', readonly=True)
    closed_pnl = fields.Monetary('P/L de posiciones cerradas', readonly=True)
    stop_loss_amount = fields.Monetary('Stop de la copia', readonly=True)
    stop_loss_pct = fields.Float('Stop de la copia (%)', readonly=True)
    position_ids = fields.One2many('trading.position', 'mirror_id', 'Posiciones')
    open_position_count = fields.Integer('Abiertas', compute='_compute_totals')
    invested = fields.Monetary('Invertido', compute='_compute_totals')
    unrealized_pnl = fields.Monetary('P/L no realizado', compute='_compute_totals')
    value = fields.Monetary('Valor actual', compute='_compute_totals')
    total_pnl = fields.Monetary('Resultado total', compute='_compute_totals')
    total_pnl_pct = fields.Float('Resultado %', compute='_compute_totals', digits=(16, 2))

    _unique_ref = models.Constraint('UNIQUE(account_id, broker_ref)', 'La cartera copiada ya existe en esta cuenta.')

    @api.depends('position_ids.state', 'position_ids.amount', 'position_ids.unrealized_pnl',
                 'available_amount', 'initial_investment', 'deposits', 'withdrawals')
    def _compute_totals(self):
        for m in self:
            open_pos = m.position_ids.filtered(lambda p: p.state == 'open')
            m.open_position_count = len(open_pos)
            m.invested = sum(open_pos.mapped('amount'))
            m.unrealized_pnl = sum(open_pos.mapped('unrealized_pnl'))
            m.value = m.available_amount + m.invested + m.unrealized_pnl
            contributed = m.initial_investment + m.deposits - m.withdrawals
            m.total_pnl = m.value - contributed if contributed else 0.0
            m.total_pnl_pct = m.total_pnl / contributed * 100 if contributed else 0.0

    def action_view_positions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('trading_base.action_trading_position')
        action['domain'] = [('mirror_id', '=', self.id)]
        action['context'] = {'search_default_open': 1}
        action['name'] = self.name
        return action
