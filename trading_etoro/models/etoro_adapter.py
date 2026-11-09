"""Adaptador de la API pública de eToro (https://api-portal.etoro.com).

Toda petición lleva ``x-api-key``, ``x-user-key`` y un ``x-request-id`` nuevo.
"""
import logging
import uuid

import requests

from odoo.addons.trading_base.models.broker_adapter import BrokerAdapter, BrokerError, register_adapter

_logger = logging.getLogger(__name__)

BASE_URL = 'https://public-api.etoro.com'
TIMEOUT = 20
SEARCH_FIELDS = 'instrumentId,internalSymbolFull,displayname,internalAssetClassId,internalAssetClassName'
SEARCH_PAGE_SIZE = 100
# internalAssetClassId de eToro → asset_type (verificado contra la API demo el 2026-11-05).
INSTRUMENT_TYPES = {1: 'forex', 2: 'commodity', 4: 'index', 5: 'stock', 6: 'etf', 10: 'crypto'}


def _num(value, default=0.0):
    if isinstance(value, dict):
        for key in ('pnL', 'pnl', 'value', 'amount'):
            if key in value:
                return _num(value[key], default)
        return default
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _first(d, *keys, default=None):
    for key in keys:
        if isinstance(d, dict) and d.get(key) is not None:
            return d[key]
    return default


@register_adapter('etoro')
class EtoroAdapter(BrokerAdapter):

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self):
        account = self.account.sudo()
        if not account.etoro_api_key or not account.etoro_user_key:
            raise BrokerError('Faltan las claves de eToro (API key y User key) en la cuenta.')
        return {
            'x-api-key': account.etoro_api_key,
            'x-user-key': account.etoro_user_key,
            'x-request-id': str(uuid.uuid4()),
            'Accept': 'application/json',
        }

    def _request(self, method, path, params=None, payload=None):
        url = BASE_URL + path
        try:
            response = requests.request(
                method, url, headers=self._headers(), params=params, json=payload, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise BrokerError(f'No se pudo contactar con eToro: {e}') from e
        status = response.status_code
        if status == 429:
            retry = response.headers.get('Retry-After', '?')
            raise BrokerError(f'eToro: límite de peticiones alcanzado; reintente en {retry} s.')
        if status in (401, 403):
            raise BrokerError('eToro rechazó las credenciales (401/403). Revise API key, User key y entorno.')
        if status >= 400:
            detail = ''
            try:
                body = response.json()
                detail = _first(body, 'message', 'errorMessage', 'error', 'title', default='') or ''
            except ValueError:
                detail = (response.text or '')[:300]
            raise BrokerError(f'eToro respondió {status}: {detail}'.strip())
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as e:
            raise BrokerError('eToro devolvió una respuesta que no es JSON.') from e

    @property
    def _env_segment(self):
        return 'demo' if self.is_demo else 'real'

    # ------------------------------------------------------------------
    # Contrato
    # ------------------------------------------------------------------
    def test_connection(self):
        data = self.get_portfolio()
        return f"Conexión correcta con eToro ({self._env_segment}). Disponible: {data['credit']:.2f}"

    def _parse_instrument(self, item):
        ref = _first(item, 'instrumentId', 'instrumentID', 'InstrumentID')
        if ref is None:
            return None
        symbol = _first(item, 'internalSymbolFull', 'symbolFull', 'symbol', default=str(ref))
        type_id = _first(item, 'internalAssetClassId', 'instrumentTypeID', 'instrumentTypeId')
        try:
            asset_type = INSTRUMENT_TYPES.get(int(type_id), 'other')
        except (TypeError, ValueError):
            asset_type = 'other'
        return {
            'broker_ref': str(ref),
            'symbol': symbol,
            'name': _first(item, 'displayname', 'displayName', 'instrumentDisplayName', default=symbol),
            'asset_type': asset_type,
        }

    def _search(self, **filters):
        params = {**filters, 'fields': SEARCH_FIELDS, 'pageSize': SEARCH_PAGE_SIZE}
        data = self._request('GET', '/api/v1/market-data/search', params=params)
        items = _first(data, 'items', 'Items', default=[]) or []
        return [r for r in (self._parse_instrument(i) for i in items) if r]

    def search_instruments(self, query):
        """La API filtra por prefijo: se devuelve primero la coincidencia exacta."""
        term = query.strip().upper()
        results = self._search(internalSymbolFull=term)
        return sorted(results, key=lambda r: (r['symbol'].upper() != term, len(r['symbol'])))

    def get_instrument_info(self, broker_ref):
        results = self._search(instrumentId=broker_ref)
        return results[0] if results else {}

    def get_rates(self, refs):
        refs = [str(r) for r in refs if r]
        rates = {}
        for start in range(0, len(refs), 1000):
            chunk = refs[start:start + 1000]
            data = self._request('GET', '/api/v2/market-data/rates', params={'instrumentIds': ','.join(chunk)})
            for r in _first(data, 'results', 'rates', default=[]) or []:
                ref = _first(r, 'instrumentId', 'instrumentID')
                if ref is None:
                    continue
                rates[str(ref)] = {
                    'bid': _num(_first(r, 'bid', 'Bid')),
                    'ask': _num(_first(r, 'ask', 'Ask')),
                    'last': _num(_first(r, 'lastExecution', 'last')) or None,
                    'date': _first(r, 'date', 'Date'),
                    'quote_type': 'delayed' if str(_first(r, 'quoteType', default='')).lower() == 'delayed' else 'realtime',
                }
        return rates

    def get_candles(self, broker_ref, interval='OneDay', count=200):
        count = max(1, min(int(count), 1000))
        data = self._request(
            'GET', f'/api/v1/market-data/instruments/{broker_ref}/history/candles/asc/{interval}/{count}')
        groups = _first(data, 'candles', default=[]) or []
        raw = []
        if groups and isinstance(groups[0], dict) and 'candles' in groups[0]:
            for g in groups:
                raw.extend(g.get('candles') or [])
        else:
            raw = groups
        candles = [{
            'date': _first(c, 'fromDate', 'date'),
            'open': _num(c.get('open')), 'high': _num(c.get('high')), 'low': _num(c.get('low')),
            'close': _num(c.get('close')), 'volume': _num(c.get('volume')),
        } for c in raw]
        return sorted(candles, key=lambda c: c['date'] or '')

    def _parse_position(self, p):
        is_buy = _first(p, 'isBuy', 'IsBuy', default=True)
        return {
            'broker_ref': str(_first(p, 'positionId', 'positionID', 'PositionID')),
            'instrument_ref': str(_first(p, 'instrumentId', 'instrumentID', 'InstrumentID')),
            'side': 'buy' if is_buy else 'sell',
            'open_date': _first(p, 'openDateTime', 'openDate'),
            'open_rate': _num(_first(p, 'openRate', 'OpenRate')),
            'amount': _num(_first(p, 'amount', 'Amount')),
            'units': _num(_first(p, 'units', 'Units')),
            'leverage': int(_num(_first(p, 'leverage', 'Leverage'), 1) or 1),
            'stop_loss': _num(_first(p, 'stopLossRate', 'StopLossRate')),
            'take_profit': _num(_first(p, 'takeProfitRate', 'TakeProfitRate')),
            'fees': _num(_first(p, 'totalFees', 'fees')),
            'broker_pnl': _num(_first(p, 'unrealizedPnL', 'pnL', 'pnl')),
        }

    def _parse_mirror(self, m):
        """Cartera copiada (CopyTrader / Smart Portfolio)."""
        return {
            'broker_ref': str(_first(m, 'mirrorID', 'mirrorId')),
            'name': _first(m, 'parentUsername', 'ParentUsername', default='') or '',
            'paused': bool(_first(m, 'isPaused', default=False)),
            'start_date': _first(m, 'startedCopyDate'),
            'initial_investment': _num(_first(m, 'initialInvestment')),
            'deposits': _num(_first(m, 'depositSummary')),
            'withdrawals': _num(_first(m, 'withdrawalSummary')),
            'available_amount': _num(_first(m, 'availableAmount')),
            'closed_pnl': _num(_first(m, 'closedPositionsNetProfit')),
            'stop_loss_amount': _num(_first(m, 'stopLossAmount')),
            'stop_loss_pct': _num(_first(m, 'stopLossPercentage')),
            'positions': [self._parse_position(p) for p in _first(m, 'positions', 'Positions', default=[]) or []],
        }

    def get_portfolio(self):
        data = self._request('GET', f'/api/v1/trading/info/{self._env_segment}/pnl')
        portfolio = _first(data, 'clientPortfolio', 'ClientPortfolio', default=data) or {}
        return {
            'credit': _num(_first(portfolio, 'credit', 'Credit')),
            'unrealized_pnl': _num(_first(portfolio, 'unrealizedPnL', 'UnrealizedPnL')),
            'positions': [self._parse_position(p) for p in _first(portfolio, 'positions', 'Positions', default=[]) or []],
            'mirrors': [self._parse_mirror(m) for m in _first(portfolio, 'mirrors', 'Mirrors', default=[]) or []],
        }

    def place_order(self, order):
        path = '/api/v2/trading/execution/demo/orders' if self.is_demo else '/api/v2/trading/execution/orders'
        body = {
            'action': 'open',
            'transaction': order.side,
            'symbol': order.instrument_id.symbol,
            'instrumentId': int(order.instrument_id.broker_ref),
            'orderType': 'mkt',
            'leverage': order.leverage or 1,
            'orderCurrency': 'usd',
        }
        if order.size_mode == 'units':
            body['units'] = order.units
        else:
            body['amount'] = order.amount
        if order.stop_loss:
            body.update(stopLossRate=order.stop_loss, stopLossType='fixed')
        if order.take_profit:
            body['takeProfitRate'] = order.take_profit
        data = self._request('POST', path, payload=body)
        return {'broker_order_id': _first(data, 'orderId', 'orderID', 'referenceId', 'token', default=''), 'raw': data}

    def close_position(self, position, units=None):
        # La ruta demo es un supuesto (no verificada en la documentación).
        segment = 'demo/' if self.is_demo else ''
        path = f'/api/v1/trading/execution/{segment}market-close-orders/positions/{position.broker_ref}'
        body = {'InstrumentId': int(position.instrument_id.broker_ref), 'UnitsToDeduct': units}
        return self._request('POST', path, payload=body)
