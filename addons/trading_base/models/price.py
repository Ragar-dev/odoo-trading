from odoo import fields, models

INTERVALS = [
    ('OneMinute', '1 min'), ('FiveMinutes', '5 min'), ('TenMinutes', '10 min'),
    ('FifteenMinutes', '15 min'), ('ThirtyMinutes', '30 min'), ('OneHour', '1 h'),
    ('FourHours', '4 h'), ('OneDay', '1 día'), ('OneWeek', '1 semana'),
]


class TradingPrice(models.Model):
    _name = 'trading.price'
    _description = 'Vela de precio'
    _order = 'instrument_id, interval, date desc'

    instrument_id = fields.Many2one('trading.instrument', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='instrument_id.company_id', store=True)
    interval = fields.Selection(INTERVALS, required=True, default='OneDay')
    date = fields.Datetime(required=True, index=True)
    open = fields.Float(digits=(16, 6))
    high = fields.Float(digits=(16, 6))
    low = fields.Float(digits=(16, 6))
    close = fields.Float(digits=(16, 6))
    volume = fields.Float(digits=(16, 2))

    _unique_candle = models.Constraint(
        'UNIQUE(instrument_id, interval, date)', 'Vela duplicada.')
