import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .broker_adapter import ADAPTERS, BrokerError, parse_datetime

_logger = logging.getLogger(__name__)


class TradingBrokerAccount(models.Model):
    _name = 'trading.broker.account'
    _description = 'Cuenta de broker'
    _inherit = ['mail.thread']
    _order = 'name'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    broker_type = fields.Selection(
        [('manual', 'Manual')], string='Broker', required=True, default='manual', tracking=True)
    environment = fields.Selection(
        [('demo', 'Demo (virtual)'), ('real', 'Real')], required=True, default='demo', tracking=True)
    allow_real_orders = fields.Boolean(
        'Permitir órdenes reales', tracking=True, groups='trading_base.group_trading_manager',
        help='Sin esta marca, una cuenta en entorno real no puede enviar ni cerrar órdenes.')
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    currency_id = fields.Many2one(
        'res.currency', string='Moneda de la cuenta', required=True,
        default=lambda s: s.env.ref('base.USD', raise_if_not_found=False) or s.env.company.currency_id)
    company_currency_id = fields.Many2one(related='company_id.currency_id')
    state = fields.Selection(
        [('draft', 'Sin probar'), ('ok', 'Conectada'), ('error', 'Error')],
        default='draft', required=True, tracking=True, copy=False)
    last_error = fields.Text(readonly=True, copy=False)
    last_sync = fields.Datetime('Última sincronización', readonly=True, copy=False)
    credit = fields.Monetary('Disponible', currency_field='currency_id', readonly=True)
    unrealized_pnl = fields.Monetary('P/L no realizado', currency_field='currency_id', readonly=True)
    equity = fields.Monetary('Patrimonio', currency_field='currency_id', readonly=True)

    journal_id = fields.Many2one(
        'account.journal', 'Diario', check_company=True,
        domain="[('type', '=', 'general')]")
    broker_asset_account_id = fields.Many2one(
        'account.account', 'Cuenta del broker (572)', check_company=True)
    gain_account_id = fields.Many2one('account.account', 'Cuenta de beneficios (766)', check_company=True)
    loss_account_id = fields.Many2one('account.account', 'Cuenta de pérdidas (666)', check_company=True)

    instrument_ids = fields.One2many('trading.instrument', 'account_id')
    position_ids = fields.One2many('trading.position', 'account_id')
    mirror_ids = fields.One2many('trading.mirror', 'account_id', 'Carteras copiadas')
    mirror_count = fields.Integer(compute='_compute_counts')
    instrument_count = fields.Integer(compute='_compute_counts')
    open_position_count = fields.Integer(compute='_compute_counts')

    def _compute_counts(self):
        for acc in self:
            acc.instrument_count = len(acc.instrument_ids)
            acc.open_position_count = len(acc.position_ids.filtered(lambda p: p.state == 'open'))
            acc.mirror_count = len(acc.mirror_ids)

    # ------------------------------------------------------------------
    # Valores contables por defecto (buscados por código PGC)
    # ------------------------------------------------------------------
    @api.model
    def _find_account(self, company, prefixes, exclude=()):
        Account = self.env['account.account'].with_company(company)
        for prefix in prefixes:
            domain = [*Account._check_company_domain(company), ('code', '=like', f'{prefix}%')]
            for acc in Account.search(domain, order='code'):
                if acc.code not in exclude:
                    return acc
        return Account

    @api.model
    def _default_accounting_vals(self, company):
        vals = {}
        journal = self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(company), ('type', '=', 'general'),
        ], limit=1)
        if journal:
            vals['journal_id'] = journal.id
        asset = self._find_account(company, ['572'], exclude=('572998',))
        gain = self._find_account(company, ['7663', '766'])
        loss = self._find_account(company, ['6663', '666'])
        if asset:
            vals['broker_asset_account_id'] = asset.id
        if gain:
            vals['gain_account_id'] = gain.id
        if loss:
            vals['loss_account_id'] = loss.id
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            company = self.env['res.company'].browse(vals.get('company_id')) or self.env.company
            for key, value in self._default_accounting_vals(company).items():
                vals.setdefault(key, value)
        return super().create(vals_list)

    def write(self, vals):
        if 'environment' in vals:
            changed = self.filtered(lambda a: a.environment != vals['environment'])
            if changed.position_ids or changed.mirror_ids:
                raise UserError(self.env._(
                    'No se puede cambiar de entorno (demo/real) una cuenta que ya tiene posiciones: se mezclarían '
                    'operaciones virtuales y reales. Cree una cuenta nueva para el otro entorno.'))
        return super().write(vals)

    # ------------------------------------------------------------------
    # Adaptador
    # ------------------------------------------------------------------
    def _get_adapter(self):
        self.ensure_one()
        cls = ADAPTERS.get(self.broker_type)
        if not cls:
            raise UserError(self.env._('No hay adaptador para el broker «%s».', self.broker_type))
        return cls(self)

    def _check_can_trade(self):
        for acc in self:
            if acc.environment == 'real' and not acc.sudo().allow_real_orders:
                raise UserError(self.env._(
                    'La cuenta «%s» es REAL y no tiene permitidas las órdenes reales.', acc.name))

    @api.model
    def _notify(self, message, kind='success', title=None):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title or self.env._('Trading'),
                'message': message,
                'type': kind,
                'sticky': kind == 'danger',
                'next': {'type': 'ir.actions.client', 'tag': 'soft_reload'},
            },
        }

    def _set_error(self, error):
        self.write({'state': 'error', 'last_error': str(error)})
        return self._notify(str(error), 'danger', self.env._('Error del broker'))

    # ------------------------------------------------------------------
    # Botones
    # ------------------------------------------------------------------
    def action_test_connection(self):
        self.ensure_one()
        try:
            message = self._get_adapter().test_connection()
        except BrokerError as e:
            return self._set_error(e)
        self.write({'state': 'ok', 'last_error': False})
        return self._notify(message or self.env._('Conexión correcta.'))

    def action_sync_portfolio(self):
        self.ensure_one()
        try:
            summary = self._sync_portfolio()
        except BrokerError as e:
            return self._set_error(e)
        return self._notify(summary)

    def action_refresh_prices(self):
        self.ensure_one()
        try:
            self.instrument_ids._refresh_prices(raise_errors=True)
        except BrokerError as e:
            return self._set_error(e)
        return self._notify(self.env._('Precios actualizados.'))

    def action_open_import_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Importar instrumentos'),
            'res_model': 'trading.instrument.import.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_account_id': self.id},
        }

    def action_view_positions(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('trading_base.action_trading_position')
        action['domain'] = [('account_id', '=', self.id)]
        action['context'] = {'default_account_id': self.id, 'search_default_open': 1}
        return action

    def action_view_mirrors(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('trading_base.action_trading_mirror')
        action['domain'] = [('account_id', '=', self.id)]
        return action

    def action_view_instruments(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('trading_base.action_trading_instrument')
        action['domain'] = [('account_id', '=', self.id)]
        action['context'] = {'default_account_id': self.id}
        return action

    # ------------------------------------------------------------------
    # Sincronización de cartera
    # ------------------------------------------------------------------
    def _get_or_create_instrument(self, adapter, ref):
        self.ensure_one()
        Instrument = self.env['trading.instrument']
        ref = str(ref)
        inst = Instrument.search([('account_id', '=', self.id), ('broker_ref', '=', ref)], limit=1)
        if inst:
            return inst
        info = {}
        try:
            info = adapter.get_instrument_info(ref) or {}
        except BrokerError as e:
            _logger.warning('Trading: sin información del instrumento %s: %s', ref, e)
        return Instrument.create({
            'account_id': self.id,
            'broker_ref': ref,
            'symbol': info.get('symbol') or ref,
            'name': info.get('name') or info.get('symbol') or ref,
            'asset_type': info.get('asset_type') or 'other',
        })

    def _sync_portfolio(self):
        """Vuelca la cartera del broker en ``trading.position``. Devuelve un resumen."""
        self.ensure_one()
        adapter = self._get_adapter()
        data = adapter.get_portfolio() or {}
        Position = self.env['trading.position']
        now = fields.Datetime.now()
        Mirror = self.env['trading.mirror'].with_context(active_test=False)
        mirrors_by_ref = {}
        for m in data.get('mirrors', []):
            mref = str(m.get('broker_ref'))
            mvals = {
                'account_id': self.id, 'broker_ref': mref, 'active': True,
                'name': m.get('name') or mref,
                'paused': bool(m.get('paused')),
                'start_date': parse_datetime(m.get('start_date')),
                'initial_investment': m.get('initial_investment') or 0.0,
                'deposits': m.get('deposits') or 0.0,
                'withdrawals': m.get('withdrawals') or 0.0,
                'available_amount': m.get('available_amount') or 0.0,
                'closed_pnl': m.get('closed_pnl') or 0.0,
                'stop_loss_amount': m.get('stop_loss_amount') or 0.0,
                'stop_loss_pct': m.get('stop_loss_pct') or 0.0,
            }
            mirror = Mirror.search([('account_id', '=', self.id), ('broker_ref', '=', mref)], limit=1)
            if mirror:
                mirror.write(mvals)
            else:
                mirror = Mirror.create(mvals)
            mirrors_by_ref[mref] = mirror
        Mirror.search([('account_id', '=', self.id), ('broker_ref', 'not in', list(mirrors_by_ref))]).active = False
        seen = Position
        created = 0
        all_positions = list(data.get('positions', []))
        for m in data.get('mirrors', []):
            for mp in m.get('positions', []):
                all_positions.append({**mp, 'mirror_ref': str(m.get('broker_ref'))})
        for p in all_positions:
            ref = str(p.get('broker_ref'))
            instrument = self._get_or_create_instrument(adapter, p.get('instrument_ref'))
            vals = {
                'account_id': self.id,
                'instrument_id': instrument.id,
                'broker_ref': ref,
                'side': p.get('side') or 'buy',
                'open_date': parse_datetime(p.get('open_date')) or now,
                'open_rate': p.get('open_rate') or 0.0,
                'amount': p.get('amount') or 0.0,
                'units': p.get('units') or 0.0,
                'leverage': p.get('leverage') or 1,
                'stop_loss': p.get('stop_loss') or 0.0,
                'take_profit': p.get('take_profit') or 0.0,
                'fees': p.get('fees') or 0.0,
                'broker_pnl': p.get('broker_pnl') or 0.0,
                'state': 'open',
                'mirror_id': mirrors_by_ref[p['mirror_ref']].id if p.get('mirror_ref') in mirrors_by_ref else False,
            }
            pos = Position.search([('account_id', '=', self.id), ('broker_ref', '=', ref)], limit=1)
            if pos:
                pos.write(vals)
            else:
                pos = Position.create(vals)
                created += 1
            seen |= pos
        gone = Position.search([
            ('account_id', '=', self.id), ('state', '=', 'open'), ('id', 'not in', seen.ids),
            ('broker_ref', '!=', False),
        ])
        for pos in gone:
            pos._close_estimated(now)
        credit = data.get('credit') or 0.0
        upnl = data.get('unrealized_pnl') or 0.0
        invested = sum(seen.mapped('amount')) + sum(m.available_amount for m in mirrors_by_ref.values())
        self.write({
            'credit': credit,
            'unrealized_pnl': upnl,
            'equity': credit + invested + upnl,
            'last_sync': now,
            'state': 'ok',
            'last_error': False,
        })
        return self.env._(
            '%(n)s posiciones abiertas (%(new)s nuevas), %(closed)s cerradas, %(m)s carteras copiadas.',
            n=len(seen), new=created, closed=len(gone), m=len(mirrors_by_ref))

    # ------------------------------------------------------------------
    # Crons
    # ------------------------------------------------------------------
    @api.model
    def _cron_sync_portfolios(self):
        for acc in self.search([('broker_type', '!=', 'manual')]):
            try:
                with self.env.cr.savepoint():
                    acc._sync_portfolio()
            except BrokerError as e:
                acc.write({'state': 'error', 'last_error': str(e)})
                _logger.warning('Trading: error sincronizando %s: %s', acc.name, e)
