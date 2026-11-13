from odoo.tests import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestDashboardHttp(HttpCase):

    def test_access(self):
        # Sin sesión: a la pantalla de acceso.
        res = self.url_open('/trading', allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn('/web/login', res.headers.get('Location', ''))

        # Usuario interno sin el grupo de Trading: prohibido.
        new_test_user(self.env, login='notrader', groups='base.group_user', password='notrader123')
        self.authenticate('notrader', 'notrader123')
        self.assertEqual(self.url_open('/trading', allow_redirects=False).status_code, 403)
        self.assertEqual(self.url_open('/trading/api/summary').status_code, 403)

        # Usuario de Trading: página y datos.
        new_test_user(self.env, login='trader', groups='base.group_user,trading_base.group_trading_user',
                      password='trader12345')
        self.authenticate('trader', 'trader12345')
        page = self.url_open('/trading')
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="td-app"', page.text)
        self.assertIn('noindex', page.headers.get('X-Robots-Tag', ''))
        data = self.url_open('/trading/api/summary').json()
        self.assertIn('totals', data)
        self.assertIn('points', self.url_open('/trading/api/history?days=7').json())
        self.assertIn('rows', self.url_open('/trading/api/closed').json())
        self.assertIn('net_result', self.url_open('/trading/api/tax').json())
