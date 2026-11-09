from odoo import fields, models


class ResUsers(models.Model):
    _inherit = 'res.users'

    trading_chart_prefs = fields.Json('Preferencias de la gráfica de trading', copy=False)
