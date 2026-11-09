import re

from odoo import api, fields, models

COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
CHART_PREFS_DEFAULT = {
    'show_markers': True, 'show_lines': True,
    'buy_color': '#26a69a', 'sell_color': '#ef5350', 'marker_size': 1,
}

DRAWING_KINDS = [
    ('hline', 'Línea horizontal'), ('trend', 'Línea de tendencia'),
    ('rect', 'Rectángulo'), ('text', 'Texto'),
]


class TradingChartDrawing(models.Model):
    _name = 'trading.chart.drawing'
    _description = 'Dibujo en la gráfica'
    _order = 'instrument_id, id'

    instrument_id = fields.Many2one('trading.instrument', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='instrument_id.company_id', store=True, index=True)
    kind = fields.Selection(DRAWING_KINDS, required=True, default='hline')
    # Tiempos en segundos UNIX (UTC). En una línea horizontal solo cuenta price1.
    time1 = fields.Integer()
    price1 = fields.Float(digits=(16, 6), required=True)
    time2 = fields.Integer()
    price2 = fields.Float(digits=(16, 6))
    color = fields.Char(default='#2962ff')
    text = fields.Char()

    def _chart_dict(self):
        return [{
            'id': d.id, 'kind': d.kind, 'time1': d.time1, 'price1': d.price1,
            'time2': d.time2, 'price2': d.price2, 'color': d.color or '#2962ff', 'text': d.text or '',
        } for d in self]

    @api.model
    def chart_create(self, vals):
        """Crea un dibujo desde la gráfica y devuelve su forma para el cliente."""
        allowed = {'instrument_id', 'kind', 'time1', 'price1', 'time2', 'price2', 'color', 'text'}
        return self.create({k: v for k, v in vals.items() if k in allowed})._chart_dict()[0]

    @api.model
    def chart_prefs(self):
        """Preferencias de marcas del usuario actual, completadas con los valores por defecto."""
        return self._clean_prefs(self.env.user.trading_chart_prefs or {})

    @api.model
    def save_chart_prefs(self, prefs):
        clean = self._clean_prefs({**self.chart_prefs(), **(prefs or {})})
        # Solo se escribe este campo del propio usuario.
        self.env.user.sudo().trading_chart_prefs = clean
        return clean

    @api.model
    def _clean_prefs(self, prefs):
        clean = dict(CHART_PREFS_DEFAULT)
        for key in ('show_markers', 'show_lines'):
            if key in prefs:
                clean[key] = bool(prefs[key])
        for key in ('buy_color', 'sell_color'):
            if isinstance(prefs.get(key), str) and COLOR_RE.match(prefs[key]):
                clean[key] = prefs[key].lower()
        try:
            clean['marker_size'] = min(3, max(1, int(prefs.get('marker_size', clean['marker_size']))))
        except (TypeError, ValueError):
            pass
        return clean
