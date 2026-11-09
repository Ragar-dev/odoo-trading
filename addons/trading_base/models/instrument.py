import logging
from datetime import timedelta, timezone

from odoo import api, fields, models

from .broker_adapter import BrokerError, parse_datetime

_logger = logging.getLogger(__name__)

# Intervalos que ofrece la gráfica propia (nombre de eToro -> segundos).
CHART_INTERVALS = {
    'FifteenMinutes': 900, 'OneHour': 3600, 'FourHours': 14400, 'OneDay': 86400, 'OneWeek': 604800,
}
CHART_CANDLES = 500

ASSET_TYPES = [
    ('stock', 'Acción'), ('etf', 'ETF'), ('crypto', 'Cripto'), ('forex', 'Divisa'),
    ('commodity', 'Materia prima'), ('index', 'Índice'), ('other', 'Otro'),
]

# Sufijo de eToro -> prefijo de mercado en TradingView ('' = quitar el sufijo).
TV_SUFFIX_EXCHANGE = {
    'US': '', 'RTH': '', 'L': 'LSE', 'DE': 'XETR', 'PA': 'EURONEXT', 'AS': 'EURONEXT',
    'BR': 'EURONEXT', 'LSB': 'EURONEXT', 'MC': 'BME', 'MI': 'MIL', 'ZU': 'SIX', 'SW': 'SIX',
    'ST': 'OMXSTO', 'OL': 'OSL', 'CO': 'OMXCOP', 'HE': 'OMXHEX', 'HK': 'HKEX', 'T': 'TSE',
    'TO': 'TSX', 'AX': 'ASX', 'NV': 'EURONEXT', 'LS': 'EURONEXT',
}
TV_INDEX = {
    'SPX500': 'SP:SPX', 'NSDQ100': 'NASDAQ:NDX', 'DJ30': 'DJ:DJI', 'GER40': 'XETR:DAX',
    'UK100': 'TVC:UKX', 'FRA40': 'EURONEXT:PX1', 'JPN225': 'TVC:NI225', 'ESP35': 'BME:IBC',
    'EUSTX50': 'TVC:SX5E', 'HKG50': 'TVC:HSI', 'AUS200': 'ASX:XJO', 'US2000': 'TVC:RUT',
    'VIX': 'TVC:VIX', 'CHINA50': 'FTSE:XINA50',
}
TV_COMMODITY = {
    'GOLD': 'TVC:GOLD', 'SILVER': 'TVC:SILVER', 'OIL': 'TVC:USOIL', 'BRENT': 'TVC:UKOIL',
    'NATGAS': 'NYMEX:NG1!', 'COPPER': 'COMEX:HG1!', 'PLATINUM': 'TVC:PLATINUM',
    'PALLADIUM': 'TVC:PALLADIUM',
}


def tradingview_symbol_for(symbol, asset_type):
    """Traduce el símbolo del broker (eToro) al de TradingView. Es una aproximación:
    el campo del instrumento se puede corregir a mano."""
    sym = (symbol or '').strip().upper()
    if not sym:
        return False
    if ':' in sym:
        return sym
    if asset_type == 'crypto':
        base = sym.split('/')[0]
        return f'BINANCE:{base}USDT' if not base.endswith('USD') else f'BINANCE:{base}T'
    if asset_type == 'forex':
        return f"FX:{sym.replace('/', '')}"
    if asset_type == 'index':
        return TV_INDEX.get(sym, sym)
    if asset_type == 'commodity':
        return TV_COMMODITY.get(sym, sym)
    if '.' in sym:
        base, suffix = sym.rsplit('.', 1)
        if suffix in TV_SUFFIX_EXCHANGE:
            exchange = TV_SUFFIX_EXCHANGE[suffix]
            return f'{exchange}:{base}' if exchange else base
    return sym


class TradingInstrument(models.Model):
    _name = 'trading.instrument'
    _description = 'Instrumento'
    _order = 'watchlist desc, symbol'
    _rec_names_search = ['symbol', 'name', 'broker_ref']

    symbol = fields.Char(required=True, index=True)
    name = fields.Char()
    broker_ref = fields.Char('ID en el broker', required=True, index=True)
    account_id = fields.Many2one('trading.broker.account', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='account_id.company_id', store=True, index=True)
    currency_id = fields.Many2one(related='account_id.currency_id')
    asset_type = fields.Selection(ASSET_TYPES, default='other', required=True)
    watchlist = fields.Boolean('En seguimiento', default=True)
    bid = fields.Float(digits=(16, 6), readonly=True)
    ask = fields.Float(digits=(16, 6), readonly=True)
    last_price = fields.Float('Precio', digits=(16, 6), readonly=True)
    prev_close = fields.Float('Cierre anterior', digits=(16, 6))
    prev_close_date = fields.Date(readonly=True)
    change_pct = fields.Float('Var. %', digits=(16, 2), readonly=True)
    tick_direction = fields.Selection(
        [('up', 'Sube'), ('down', 'Baja'), ('flat', 'Igual')], default='flat', readonly=True)
    quote_type = fields.Selection(
        [('realtime', 'Tiempo real'), ('delayed', 'Diferido')], readonly=True)
    price_date = fields.Datetime('Fecha del precio', readonly=True)
    price_checked_at = fields.Datetime('Consultado', readonly=True)
    price_ids = fields.One2many('trading.price', 'instrument_id', 'Velas')
    drawing_ids = fields.One2many('trading.chart.drawing', 'instrument_id', 'Dibujos')
    candles_checked_at = fields.Datetime('Velas consultadas', readonly=True)
    tradingview_symbol = fields.Char(
        'Símbolo TradingView', compute='_compute_tradingview_symbol', store=True, readonly=False,
        help='Símbolo de la gráfica en TradingView (p. ej. NASDAQ:AAPL). Se calcula solo; '
             'corríjalo a mano si la gráfica no carga el activo correcto.')

    _unique_ref = models.Constraint(
        'UNIQUE(account_id, broker_ref)', 'El instrumento ya existe en esta cuenta.')

    @api.depends('symbol', 'name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.symbol} — {rec.name}' if rec.name and rec.name != rec.symbol else rec.symbol

    @api.depends('symbol', 'asset_type')
    def _compute_tradingview_symbol(self):
        for rec in self:
            rec.tradingview_symbol = tradingview_symbol_for(rec.symbol, rec.asset_type)

    # ------------------------------------------------------------------
    def _refresh_prices(self, raise_errors=False):
        """Pide al broker bid/ask de los instrumentos, agrupados por cuenta."""
        now = fields.Datetime.now()
        today = fields.Date.context_today(self)
        for account in self.account_id:
            instruments = self.filtered(lambda i: i.account_id == account)
            try:
                adapter = account._get_adapter()
                rates = adapter.get_rates(instruments.mapped('broker_ref')) or {}
            except BrokerError as e:
                if raise_errors:
                    raise
                account.write({'state': 'error', 'last_error': str(e)})
                _logger.warning('Trading: error de precios en %s: %s', account.name, e)
                continue
            for inst in instruments:
                rate = rates.get(inst.broker_ref)
                if not rate:
                    continue
                bid = rate.get('bid') or 0.0
                ask = rate.get('ask') or 0.0
                last = rate.get('last') or ((bid + ask) / 2 if bid and ask else bid or ask)
                vals = {
                    'bid': bid, 'ask': ask, 'last_price': last,
                    'quote_type': rate.get('quote_type') or False,
                    'price_date': parse_datetime(rate.get('date')) or now,
                    'price_checked_at': now,
                    'tick_direction': inst._tick(inst.last_price, last),
                }
                if inst.prev_close_date != today:
                    prev = inst._fetch_prev_close(adapter)
                    if prev:
                        vals.update(prev_close=prev, prev_close_date=today)
                prev_close = vals.get('prev_close', inst.prev_close)
                vals['change_pct'] = (last - prev_close) / prev_close * 100 if prev_close else 0.0
                inst.write(vals)
        return True

    @staticmethod
    def _tick(old, new):
        if not old or new == old:
            return 'flat'
        return 'up' if new > old else 'down'

    def _fetch_prev_close(self, adapter):
        """Cierre de la última vela diaria completa (la penúltima)."""
        self.ensure_one()
        try:
            candles = adapter.get_candles(self.broker_ref, 'OneDay', 2) or []
        except BrokerError:
            return 0.0
        if len(candles) >= 2:
            return candles[-2].get('close') or 0.0
        return candles[0].get('close') if candles else 0.0

    def action_refresh_prices(self):
        try:
            self._refresh_prices(raise_errors=True)
        except BrokerError as e:
            return self.env['trading.broker.account']._notify(str(e), 'danger')
        return self.env['trading.broker.account']._notify(self.env._('Precios actualizados.'))

    def _load_candles(self, interval='OneDay', count=200):
        """Descarga velas del broker y las guarda. Lanza BrokerError si falla."""
        Price = self.env['trading.price']
        total = 0
        for inst in self:
            candles = inst.account_id._get_adapter().get_candles(inst.broker_ref, interval, count) or []
            existing = {
                p.date: p for p in Price.search([
                    ('instrument_id', '=', inst.id), ('interval', '=', interval)])
            }
            to_create = []
            seen = set()
            for c in candles:
                date = parse_datetime(c.get('date'))
                if not date:
                    continue
                vals = {k: c.get(k) or 0.0 for k in ('open', 'high', 'low', 'close', 'volume')}
                if date in existing:
                    existing[date].write(vals)
                elif date not in seen:
                    to_create.append({**vals, 'instrument_id': inst.id, 'interval': interval, 'date': date})
                seen.add(date)
            Price.create(to_create)
            total += len(candles)
            inst.candles_checked_at = fields.Datetime.now()
            if interval == 'OneDay' and len(candles) >= 2:
                inst.write({'prev_close': candles[-2].get('close') or 0.0,
                            'prev_close_date': fields.Date.context_today(self)})
        return total

    def action_load_candles(self, interval='OneDay', count=200):
        try:
            total = self._load_candles(interval, count)
        except BrokerError as e:
            return self.env['trading.broker.account']._notify(str(e), 'danger')
        return self.env['trading.broker.account']._notify(self.env._('%s velas cargadas.', total))

    # ------------------------------------------------------------------
    # Gráfica propia (widget trading_chart)
    # ------------------------------------------------------------------
    def get_chart_data(self, interval='OneDay', position_id=False):
        """Velas, dibujos y marcas de posiciones para la gráfica de Odoo.
        Si faltan velas o están viejas, las pide al broker (como mucho cada 5 min)."""
        self.ensure_one()
        if interval not in CHART_INTERVALS:
            interval = 'OneDay'
        step = CHART_INTERVALS[interval]
        Price = self.env['trading.price']
        domain = [('instrument_id', '=', self.id), ('interval', '=', interval)]
        last = Price.search(domain, order='date desc', limit=1)
        now = fields.Datetime.now()
        stale = not last or last.date < now - timedelta(seconds=step * 2)
        recently = self.candles_checked_at and self.candles_checked_at > now - timedelta(minutes=5)
        error = ''
        if stale and (not last or not recently):
            try:
                self._load_candles(interval, CHART_CANDLES)
            except BrokerError as e:
                error = str(e)
                _logger.warning('Velas de %s (%s): %s', self.symbol, interval, e)
        candles = Price.search(domain, order='date asc')
        Position = self.env['trading.position']
        positions = Position.browse(position_id).exists() if position_id else Position.search(
            [('instrument_id', '=', self.id)], order='open_date asc')
        return {
            'instrument_id': self.id,
            'symbol': self.symbol,
            'name': self.name or '',
            'interval': interval,
            'step': step,
            'last_price': self.last_price,
            'error': error,
            'candles': [{
                'time': int(c.date.replace(tzinfo=timezone.utc).timestamp()),
                'open': c.open, 'high': c.high, 'low': c.low, 'close': c.close, 'volume': c.volume,
            } for c in candles],
            'drawings': self.drawing_ids._chart_dict(),
            'prefs': self.env['trading.chart.drawing'].chart_prefs(),
            'positions': [p._chart_dict(focus=bool(position_id)) for p in positions],
        }

    def action_view_prices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('trading_base.action_trading_price')
        action['domain'] = [('instrument_id', '=', self.id)]
        action['context'] = {'default_instrument_id': self.id}
        return action

    def action_new_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'trading.order',
            'view_mode': 'form',
            'target': 'current',
            'context': {'default_account_id': self.account_id.id, 'default_instrument_id': self.id},
        }

    # ------------------------------------------------------------------
    # Panel en vivo
    # ------------------------------------------------------------------
    @api.model
    def get_live_board(self):
        """Datos del panel. Refresca solo los instrumentos cuyo precio esté caducado."""
        seconds = int(self.env['ir.config_parameter'].sudo().get_param('trading.live_refresh_seconds', 5))
        limit = fields.Datetime.now() - timedelta(seconds=seconds)
        instruments = self.search([('watchlist', '=', True)])
        positions = self.env['trading.position'].search([('state', '=', 'open')])
        stale = (instruments | positions.instrument_id).filtered(
            lambda i: not i.price_checked_at or i.price_checked_at < limit)
        if stale:
            stale._refresh_prices()
        accounts = self.env['trading.broker.account'].search([])
        return {
            'refresh_seconds': seconds,
            'server_time': fields.Datetime.to_string(fields.Datetime.now()),
            'accounts': [{
                'id': a.id, 'name': a.name, 'environment': a.environment, 'state': a.state,
                'currency': a.currency_id.name, 'credit': a.credit,
                'unrealized_pnl': sum(a.position_ids.filtered(lambda p: p.state == 'open').mapped('unrealized_pnl')),
                'equity': a.equity, 'last_error': a.last_error or '',
            } for a in accounts],
            'instruments': [{
                'id': i.id, 'symbol': i.symbol, 'name': i.name or '', 'account': i.account_id.name,
                'asset_type': i.asset_type, 'bid': i.bid, 'ask': i.ask, 'last_price': i.last_price,
                'change_pct': i.change_pct, 'tick': i.tick_direction, 'quote_type': i.quote_type or '',
                'price_date': fields.Datetime.to_string(i.price_date) if i.price_date else '',
            } for i in instruments],
            'positions': [{
                'id': p.id, 'symbol': p.instrument_id.symbol, 'account': p.account_id.name,
                'side': p.side, 'units': p.units, 'mirror': p.mirror_id.name or '', 'amount': p.amount, 'leverage': p.leverage,
                'open_rate': p.open_rate, 'current_rate': p.current_rate,
                'unrealized_pnl': p.unrealized_pnl, 'currency': p.currency_id.name,
            } for p in positions],
        }

    @api.model
    def _cron_refresh_prices(self):
        ids = self.search([('watchlist', '=', True)]) | self.env['trading.position'].search(
            [('state', '=', 'open')]).instrument_id
        ids.filtered(lambda i: i.account_id.broker_type != 'manual')._refresh_prices()
