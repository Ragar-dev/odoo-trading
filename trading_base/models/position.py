from datetime import timezone

from odoo import api, fields, models
from odoo.exceptions import UserError

from .broker_adapter import BrokerError

CFD_ASSETS = ('forex', 'commodity', 'index')


class TradingPosition(models.Model):
    _name = 'trading.position'
    _description = 'Posición'
    _inherit = ['mail.thread']
    _order = 'state, open_date desc'
    _check_company_auto = True

    name = fields.Char(compute='_compute_name', store=True)
    account_id = fields.Many2one('trading.broker.account', required=True, ondelete='restrict', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='account_id.currency_id', store=True)
    company_currency_id = fields.Many2one(related='company_id.currency_id')
    instrument_id = fields.Many2one(
        'trading.instrument', required=True, ondelete='restrict',
        domain="[('account_id', '=', account_id)]")
    asset_type = fields.Selection(related='instrument_id.asset_type', store=True)
    tradingview_symbol = fields.Char(related='instrument_id.tradingview_symbol')
    broker_ref = fields.Char('ID en el broker', index=True, copy=False)
    mirror_id = fields.Many2one(
        'trading.mirror', 'Cartera copiada', index=True, ondelete='set null',
        help='La posición la gestiona el broker dentro de una cartera copiada (CopyTrader / Smart Portfolio).')
    side = fields.Selection([('buy', 'Largo'), ('sell', 'Corto')], required=True, default='buy')
    open_date = fields.Datetime(required=True, default=fields.Datetime.now)
    open_rate = fields.Float('Precio de apertura', digits=(16, 6), required=True)
    units = fields.Float(digits=(16, 6))
    amount = fields.Monetary('Importe invertido', currency_field='currency_id')
    leverage = fields.Integer('Apalancamiento', default=1)
    stop_loss = fields.Float(digits=(16, 6))
    take_profit = fields.Float(digits=(16, 6))
    fees = fields.Monetary('Comisiones', currency_field='currency_id')
    state = fields.Selection([('open', 'Abierta'), ('closed', 'Cerrada')], default='open', required=True, tracking=True)
    close_date = fields.Datetime(tracking=True)
    close_rate = fields.Float('Precio de cierre', digits=(16, 6), tracking=True)
    close_estimated = fields.Boolean(
        'Cierre estimado', help='El broker ya no devuelve la posición; el cierre se estimó con el último precio.')
    realized_pnl = fields.Monetary('P/L realizado', currency_field='currency_id', tracking=True)
    broker_pnl = fields.Monetary('P/L según broker', currency_field='currency_id')
    current_rate = fields.Float('Precio actual', digits=(16, 6), compute='_compute_live', store=True)
    unrealized_pnl = fields.Monetary(
        'P/L no realizado', currency_field='currency_id', compute='_compute_live', store=True)
    current_value = fields.Monetary(
        'Valor actual', currency_field='currency_id', compute='_compute_live', store=True,
        help='Importe invertido ± ganancia o pérdida (no realizada si está abierta, realizada si está cerrada).')
    tax_class = fields.Selection(
        [('securities', 'Transmisión de valores'), ('cfd', 'Derivado / CFD'), ('crypto', 'Criptoactivo')],
        string='Clase fiscal', compute='_compute_tax_class', store=True, readonly=False,
        help='Orientativo para la renta (IRPF, base del ahorro). Revíselo con su asesor.')
    realized_pnl_company = fields.Monetary(
        'P/L realizado (moneda compañía)', currency_field='company_currency_id',
        compute='_compute_realized_pnl_company', store=True)
    fiscal_year = fields.Integer('Ejercicio', compute='_compute_fiscal_year', store=True)
    move_id = fields.Many2one('account.move', 'Asiento', readonly=True, copy=False, check_company=True)

    def _chart_dict(self, focus=False):
        """Marca de la posición en la gráfica. Stop y take profit solo si tienen sentido:
        eToro pone valores extremos (p. ej. 0,0001) cuando no hay ninguno."""
        self.ensure_one()

        def sane(level):
            return level if level and self.open_rate and 0.2 * self.open_rate <= level <= 5 * self.open_rate else 0.0

        def ts(dt):
            return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt else 0

        return {
            'id': self.id, 'name': self.name, 'side': self.side, 'state': self.state, 'focus': focus,
            'open_time': ts(self.open_date), 'open_rate': self.open_rate,
            'close_time': ts(self.close_date) if self.state == 'closed' else 0,
            'close_rate': self.close_rate if self.state == 'closed' else 0.0,
            'stop_loss': sane(self.stop_loss), 'take_profit': sane(self.take_profit),
            'units': self.units, 'mirror': bool(self.mirror_id),
        }

    @api.depends('instrument_id.symbol', 'side', 'open_date')
    def _compute_name(self):
        for p in self:
            side = dict(self._fields['side'].selection).get(p.side, '')
            date = fields.Date.to_string(p.open_date.date()) if p.open_date else ''
            p.name = f"{p.instrument_id.symbol or '?'} {side} {date}".strip()

    def _effective_units(self):
        self.ensure_one()
        if self.units:
            return self.units
        if self.open_rate:
            return (self.amount or 0.0) * (self.leverage or 1) / self.open_rate
        return 0.0

    def _pnl_at(self, rate):
        self.ensure_one()
        direction = 1 if self.side == 'buy' else -1
        return self._effective_units() * (rate - self.open_rate) * direction

    @api.depends('instrument_id.last_price', 'instrument_id.bid', 'instrument_id.ask',
                 'open_rate', 'units', 'amount', 'leverage', 'side', 'state', 'close_rate', 'realized_pnl')
    def _compute_live(self):
        for p in self:
            if p.state == 'closed':
                p.current_rate = p.close_rate
                p.unrealized_pnl = 0.0
                p.current_value = (p.amount or 0.0) + (p.realized_pnl or 0.0)
                continue
            inst = p.instrument_id
            # Un largo se cierra vendiendo (bid); un corto, comprando (ask).
            rate = (inst.bid if p.side == 'buy' else inst.ask) or inst.last_price
            p.current_rate = rate
            p.unrealized_pnl = p._pnl_at(rate) if rate else 0.0
            p.current_value = (p.amount or 0.0) + p.unrealized_pnl

    @api.depends('leverage', 'side', 'instrument_id.asset_type')
    def _compute_tax_class(self):
        for p in self:
            asset = p.instrument_id.asset_type
            if (p.leverage or 1) > 1 or p.side == 'sell' or asset in CFD_ASSETS:
                p.tax_class = 'cfd'
            elif asset == 'crypto':
                p.tax_class = 'crypto'
            else:
                p.tax_class = 'securities'

    @api.depends('realized_pnl', 'close_date', 'currency_id', 'company_id', 'state')
    def _compute_realized_pnl_company(self):
        for p in self:
            if p.state != 'closed' or not p.currency_id or not p.company_id:
                p.realized_pnl_company = 0.0
                continue
            date = (p.close_date or fields.Datetime.now()).date()
            p.realized_pnl_company = p.currency_id._convert(
                p.realized_pnl, p.company_id.currency_id, p.company_id, date)

    @api.depends('close_date')
    def _compute_fiscal_year(self):
        for p in self:
            p.fiscal_year = p.close_date.year if p.close_date else 0

    # ------------------------------------------------------------------
    def _close_estimated(self, when):
        for p in self:
            inst = p.instrument_id
            rate = (inst.bid if p.side == 'buy' else inst.ask) or inst.last_price or p.open_rate
            p.write({
                'state': 'closed', 'close_date': when, 'close_rate': rate,
                'realized_pnl': p._pnl_at(rate) - (p.fees or 0.0),
                'close_estimated': True,
            })
            p.message_post(body=self.env._('Cerrada fuera de Odoo; P/L estimado con el último precio.'))

    def action_close_position(self):
        self.ensure_one()
        if self.state != 'open':
            raise UserError(self.env._('La posición ya está cerrada.'))
        if self.mirror_id:
            raise UserError(self.env._(
                'La posición pertenece a la cartera copiada «%s»; ciérrela desde eToro.', self.mirror_id.name))
        account = self.account_id
        account._check_can_trade()
        try:
            account._get_adapter().close_position(self)
        except BrokerError as e:
            return account._notify(str(e), 'danger', self.env._('No se pudo cerrar'))
        self.message_post(body=self.env._('Orden de cierre enviada al broker.'))
        try:
            account._sync_portfolio()
        except BrokerError:
            pass
        return account._notify(self.env._('Orden de cierre enviada.'))

    def action_close_manual(self):
        """Cierre de posiciones de cuentas manuales con el precio indicado o el actual."""
        for p in self.filtered(lambda r: r.state == 'open'):
            rate = p.close_rate or p.current_rate or p.open_rate
            p.write({
                'state': 'closed', 'close_date': p.close_date or fields.Datetime.now(),
                'close_rate': rate, 'realized_pnl': p._pnl_at(rate) - (p.fees or 0.0),
            })

    def action_create_move(self):
        Move = self.env['account.move']
        for p in self:
            if p.state != 'closed':
                raise UserError(self.env._('Solo se contabilizan posiciones cerradas (%s).', p.name))
            if p.move_id:
                continue
            acc = p.account_id
            if not (acc.journal_id and acc.broker_asset_account_id and acc.gain_account_id and acc.loss_account_id):
                raise UserError(self.env._(
                    'Configure diario y cuentas contables en la cuenta de broker «%s».', acc.name))
            amount = p.company_currency_id.round(p.realized_pnl_company)
            if p.company_currency_id.is_zero(amount):
                continue
            label = self.env._('Trading %s', p.name)
            if amount > 0:
                debit_acc, credit_acc = acc.broker_asset_account_id, acc.gain_account_id
            else:
                debit_acc, credit_acc = acc.loss_account_id, acc.broker_asset_account_id
            value = abs(amount)
            move = Move.create({
                'move_type': 'entry',
                'journal_id': acc.journal_id.id,
                'date': p.close_date.date(),
                'ref': label,
                'company_id': p.company_id.id,
                'line_ids': [
                    (0, 0, {'name': label, 'account_id': debit_acc.id, 'debit': value, 'credit': 0.0}),
                    (0, 0, {'name': label, 'account_id': credit_acc.id, 'debit': 0.0, 'credit': value}),
                ],
            })
            move.action_post()
            p.move_id = move
        return True

    def action_view_move(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'account.move',
            'res_id': self.move_id.id, 'view_mode': 'form',
        }
