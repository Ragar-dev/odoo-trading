from odoo import api, fields, models


class TradingIncome(models.Model):
    """Dividendos e intereses cobrados en el broker (rendimientos del capital mobiliario)."""
    _name = 'trading.income'
    _description = 'Dividendo o interés'
    _order = 'date desc, id desc'

    account_id = fields.Many2one('trading.broker.account', 'Cuenta', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='account_id.currency_id', store=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id')
    kind = fields.Selection([('dividend', 'Dividendo'), ('interest', 'Interés')], 'Tipo', required=True,
                            default='dividend')
    date = fields.Date('Fecha de cobro', required=True, index=True)
    instrument_id = fields.Many2one('trading.instrument', 'Instrumento', ondelete='set null')
    name = fields.Char('Descripción', required=True)
    isin = fields.Char('ISIN')
    country_code = fields.Char('País', compute='_compute_country_code', store=True,
                               help='País del emisor, tomado del ISIN.')
    broker_ref = fields.Char('Ref. broker', index=True)
    gross = fields.Monetary('Bruto', currency_field='currency_id')
    withholding = fields.Monetary('Retención en origen', currency_field='currency_id')
    net = fields.Monetary('Neto', currency_field='currency_id', compute='_compute_net', store=True)
    withholding_rate = fields.Float('Retención %', digits=(16, 2), compute='_compute_net', store=True)
    gross_company = fields.Monetary('Bruto (moneda compañía)', currency_field='company_currency_id',
                                    compute='_compute_company_amounts', store=True)
    withholding_company = fields.Monetary('Retención (moneda compañía)', currency_field='company_currency_id',
                                          compute='_compute_company_amounts', store=True)
    fiscal_year = fields.Integer('Ejercicio', compute='_compute_fiscal_year', store=True)

    @api.depends('isin')
    def _compute_country_code(self):
        for rec in self:
            code = (rec.isin or '').strip()[:2].upper()
            rec.country_code = code if code.isalpha() and len(code) == 2 else False

    @api.depends('gross', 'withholding')
    def _compute_net(self):
        for rec in self:
            rec.net = (rec.gross or 0.0) - (rec.withholding or 0.0)
            rec.withholding_rate = 100.0 * rec.withholding / rec.gross if rec.gross else 0.0

    @api.depends('gross', 'withholding', 'date', 'currency_id', 'company_id')
    def _compute_company_amounts(self):
        for rec in self:
            if not (rec.currency_id and rec.company_id and rec.date):
                rec.gross_company = rec.withholding_company = 0.0
                continue
            target, company = rec.company_id.currency_id, rec.company_id
            rec.gross_company = rec.currency_id._convert(rec.gross, target, company, rec.date)
            rec.withholding_company = rec.currency_id._convert(rec.withholding, target, company, rec.date)

    @api.depends('date')
    def _compute_fiscal_year(self):
        for rec in self:
            rec.fiscal_year = rec.date.year if rec.date else 0
