"""Web interna de trading: /trading (solo usuarios con sesión y del grupo Trading)."""
from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.http import request

GROUP = 'trading_base.group_trading_user'


def _check_access():
    if not request.env.user.has_group(GROUP):
        raise Forbidden()
    return request.env['trading.dashboard']


class TradingDashboardController(http.Controller):

    @http.route(['/trading', '/trading/<path:subpath>'], type='http', auth='user', methods=['GET'])
    def page(self, subpath=None, **kw):
        _check_access()
        return request.render('trading_base.dashboard_page', {
            'user_name': request.env.user.name,
            'company_currency': request.env.company.currency_id.name,
        }, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow',
                    'Referrer-Policy': 'same-origin'})

    @http.route('/trading/api/summary', type='http', auth='user', methods=['GET'])
    def summary(self, **kw):
        return request.make_json_response(_check_access().get_summary(), headers={'Cache-Control': 'no-store'})

    @http.route('/trading/api/history', type='http', auth='user', methods=['GET'])
    def history(self, days=30, **kw):
        return request.make_json_response(_check_access().get_history(days), headers={'Cache-Control': 'no-store'})

    @http.route('/trading/api/closed', type='http', auth='user', methods=['GET'])
    def closed(self, year=None, **kw):
        return request.make_json_response(_check_access().get_closed(year), headers={'Cache-Control': 'no-store'})

    @http.route('/trading/api/tax', type='http', auth='user', methods=['GET'])
    def tax(self, year=None, **kw):
        return request.make_json_response(_check_access().get_tax(year), headers={'Cache-Control': 'no-store'})

    @http.route('/trading/api/refresh', type='jsonrpc', auth='user', methods=['POST'])
    def refresh(self, **kw):
        _check_access()
        action = request.env['trading.position'].action_refresh_from_broker()
        params = action.get('params', {})
        return {'ok': params.get('type') != 'danger', 'message': params.get('message', '')}
