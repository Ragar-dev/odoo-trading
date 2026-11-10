from odoo import api, fields, models
from odoo.exceptions import UserError


class TradingTaxReportWizard(models.TransientModel):
    _name = 'trading.tax.report.wizard'
    _description = 'Informe para la renta (PDF)'

    @api.model
    def _default_year(self):
        return fields.Date.context_today(self).year

    year = fields.Integer('Ejercicio', required=True, default=_default_year)
    account_ids = fields.Many2many(
        'trading.broker.account', string='Cuentas',
        help='Déjelo vacío para incluir todas las cuentas.')
    company_currency_id = fields.Many2one('res.currency', default=lambda self: self.env.company.currency_id)
    other_savings = fields.Monetary(
        'Otras rentas del ahorro', currency_field='company_currency_id',
        default=lambda self: float(self.env['ir.config_parameter'].sudo().get_param('trading.other_savings') or 0.0),
        help='Rentas del ahorro fuera del trading en el mismo año (intereses del banco, fondos, otras ventas...). '
             'Solo sirve para situar el trading en su tramo del IRPF.')
    gross_result = fields.Monetary('Resultado antes de impuestos', currency_field='company_currency_id',
                                   compute='_compute_after_tax')
    tax_estimate = fields.Monetary('IRPF estimado', currency_field='company_currency_id', compute='_compute_after_tax')
    foreign_withholding = fields.Monetary('Retenciones en el extranjero', currency_field='company_currency_id',
                                          compute='_compute_after_tax')
    net_result = fields.Monetary('Neto después de impuestos', currency_field='company_currency_id',
                                 compute='_compute_after_tax')
    effective_rate = fields.Float('Tipo efectivo %', digits=(16, 2), compute='_compute_after_tax')
    marginal_rate = fields.Float('Tipo marginal %', digits=(16, 2), compute='_compute_after_tax')
    estimated_count = fields.Integer(compute='_compute_counts')
    position_count = fields.Integer(compute='_compute_counts')

    def _domain(self):
        self.ensure_one()
        domain = [('state', '=', 'closed'), ('fiscal_year', '=', self.year)]
        if self.account_ids:
            domain.append(('account_id', 'in', self.account_ids.ids))
        return domain

    @api.depends('year', 'account_ids')
    def _compute_counts(self):
        Position = self.env['trading.position']
        for w in self:
            positions = Position.search(w._domain())
            w.position_count = len(positions)
            w.estimated_count = len(positions.filtered(lambda p: p.close_estimated and not p.broker_confirmed))

    @api.depends('year', 'account_ids', 'other_savings')
    def _compute_after_tax(self):
        Report = self.env['report.trading_base.report_tax_year']
        Account = self.env['trading.broker.account'].with_context(active_test=False)
        for w in self:
            data = Report._tax_data(w.year, w.account_ids or Account.search([]), other_savings=w.other_savings)
            net = data['after_tax']
            w.gross_result = net['gross_result']
            w.tax_estimate = net['tax']
            w.foreign_withholding = net['foreign_withholding']
            w.net_result = net['net_result']
            w.effective_rate = net['effective_rate']
            w.marginal_rate = net['marginal_rate']

    def _ensure_rates(self):
        """Carga del BCE los cambios que faltan desde la compra más antigua que pueda intervenir."""
        Position = self.env['trading.position']
        domain = [('open_date', '!=', False)]
        if self.account_ids:
            domain.append(('account_id', 'in', self.account_ids.ids))
        first = Position.search(domain, order='open_date asc', limit=1)
        dates = [first.open_date.date()] if first else []
        incomes = self.env['trading.income'].search([('fiscal_year', '=', self.year)], order='date asc', limit=1)
        dates += incomes.mapped('date')
        if dates:
            self.env['res.currency']._trading_ensure_ecb_history(min(dates))

    def action_print(self):
        self.ensure_one()
        has_income = self.env['trading.income'].search_count([('fiscal_year', '=', self.year)], limit=1)
        if not self.position_count and not has_income:
            raise UserError(self.env._('No hay operaciones cerradas ni dividendos en %s.', self.year))
        self._ensure_rates()
        # Se recuerda para el IRPF estimado de las posiciones.
        self.env['ir.config_parameter'].sudo().set_param('trading.other_savings', self.other_savings or 0.0)
        return self.env.ref('trading_base.action_report_trading_tax').report_action(
            self, data={'year': self.year, 'account_ids': self.account_ids.ids, 'other_savings': self.other_savings})
