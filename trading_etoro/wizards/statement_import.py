"""Importación del extracto de cuenta de eToro (Account Statement, .xlsx).

Lee la hoja de posiciones cerradas y sustituye los cierres estimados por los reales.
eToro no documenta los nombres de columna y los cambia entre versiones e idiomas, así que
las columnas se reconocen por varios alias. Las fechas del extracto se interpretan en UTC.
"""
import base64
import io
import re
import unicodedata
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError

SHEET_NAMES = ('closed positions', 'posiciones cerradas', 'operaciones cerradas')

COLUMNS = {
    'position_id': ('position id', 'id de posicion', 'id posicion', 'posicion id', 'id de la posicion'),
    'action': ('action', 'accion', 'activo', 'instrument', 'instrumento'),
    'side': ('long / short', 'long/short', 'largo / corto', 'largo/corto', 'direction', 'direccion'),
    'amount': ('amount', 'importe', 'invested', 'invertido', 'cantidad'),
    'units': ('units', 'units / contracts', 'unidades', 'unidades / contratos'),
    'open_date': ('open date', 'fecha de apertura', 'apertura'),
    'close_date': ('close date', 'fecha de cierre', 'cierre'),
    'leverage': ('leverage', 'apalancamiento'),
    'spread': ('spread fees (usd)', 'spread fees', 'spread', 'comisiones de spread', 'diferencial'),
    'open_rate': ('open rate', 'tasa de apertura', 'precio de apertura', 'tipo de apertura'),
    'close_rate': ('close rate', 'tasa de cierre', 'precio de cierre', 'tipo de cierre'),
    'overnight': ('overnight fees and dividends', 'rollover fees and dividends', 'overnight fees',
                  'comisiones nocturnas y dividendos', 'comisiones overnight y dividendos', 'comisiones nocturnas'),
    'copied_from': ('copied from', 'copiado de'),
}
PROFIT_PREFIXES = ('profit', 'ganancia', 'beneficio', 'resultado')
DIVIDEND_SHEETS = ('dividends', 'dividendos')
ACTIVITY_SHEETS = ('account activity', 'actividad de la cuenta', 'actividad de cuenta', 'actividad')

DIVIDEND_COLUMNS = {
    'date': ('date of payment', 'fecha de pago', 'payment date', 'date', 'fecha'),
    'name': ('instrument name', 'nombre del instrumento', 'instrument', 'instrumento', 'name', 'nombre'),
    'position_id': COLUMNS['position_id'],
    'isin': ('isin',),
}
DIVIDEND_PREFIXES = {
    'net': ('net dividend received', 'dividendo neto recibido', 'dividendo neto', 'net dividend'),
    'withholding': ('withholding tax amount', 'importe de la retencion', 'importe de retencion',
                    'retencion fiscal', 'importe retenido'),
}
ACTIVITY_COLUMNS = {
    'date': ('date', 'fecha'),
    'type': ('type', 'tipo'),
    'details': ('details', 'detalles', 'detalle'),
    'amount': ('amount', 'importe', 'cantidad'),
    'position_id': COLUMNS['position_id'],
}


def _norm(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', text).strip().lower()


def _num(value):
    if value in (None, ''):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace('\xa0', '').replace(' ', '')
    negative = text.startswith('(') and text.endswith(')')
    text = text.strip('()').replace('$', '').replace('€', '').replace('%', '')
    if ',' in text and '.' in text:
        # El último separador es el decimal.
        if text.rfind(',') > text.rfind('.'):
            text = text.replace('.', '').replace(',', '.')
        else:
            text = text.replace(',', '')
    elif ',' in text:
        text = text.replace(',', '.')
    try:
        number = float(text)
    except ValueError:
        return 0.0
    return -number if negative else number


def _date(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or '').strip()
    for fmt in ('%d/%m/%Y %H:%M:%S', '%d/%m/%Y %H:%M', '%d/%m/%Y', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S',
                '%d.%m.%Y %H:%M:%S', '%d-%m-%Y %H:%M:%S'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class TradingEtoroStatementImport(models.TransientModel):
    _name = 'trading.etoro.statement.import'
    _description = 'Importar extracto de eToro'

    account_id = fields.Many2one(
        'trading.broker.account', 'Cuenta', required=True, domain=[('broker_type', '=', 'etoro')],
        default=lambda self: self.env['trading.broker.account'].search([('broker_type', '=', 'etoro')], limit=1))
    file = fields.Binary('Extracto (.xlsx)', required=True)
    filename = fields.Char()
    state = fields.Selection([('draft', 'Borrador'), ('done', 'Hecho')], default='draft')
    result = fields.Text('Resultado', readonly=True)

    # ------------------------------------------------------------------
    def _load_book(self):
        try:
            import openpyxl
        except ImportError as e:
            raise UserError(self.env._('Falta la librería openpyxl en el servidor.')) from e
        try:
            return openpyxl.load_workbook(io.BytesIO(base64.b64decode(self.file)), read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001 - cualquier fichero no válido
            raise UserError(self.env._('No se pudo leer el fichero. Descargue el extracto en formato Excel (.xlsx).')) from e

    @staticmethod
    def _sheet(book, names):
        return next((ws for ws in book.worksheets if _norm(ws.title) in names), None)

    @staticmethod
    def _sheet_table(sheet, columns, required):
        """Localiza la fila de cabeceras (la primera que contenga la columna ``required``) y
        devuelve (cabeceras normalizadas, índice de columnas, filas de datos)."""
        rows = list(sheet.iter_rows(values_only=True))
        header_index = next((i for i, row in enumerate(rows[:20])
                             if any(_norm(c) in columns[required] for c in row)), None)
        if header_index is None:
            return None, {}, []
        headers = [_norm(c) for c in rows[header_index]]
        index = {}
        for key, aliases in columns.items():
            for alias in aliases:
                if alias in headers:
                    index[key] = headers.index(alias)
                    break
        return headers, index, rows[header_index + 1:]

    def _pick(self, headers, prefixes):
        """Columna cuyo nombre empieza por un prefijo; si hay varias, la de la divisa de la cuenta."""
        candidates = [i for i, h in enumerate(headers) if h.startswith(prefixes)]
        if not candidates:
            return None
        code = (self.account_id.currency_id.name or 'usd').lower()
        return next((i for i in candidates if code in headers[i]),
                    next((i for i in candidates if '(' not in headers[i]), candidates[0]))

    def _read_rows(self, book=None):
        """Filas de la hoja de posiciones cerradas como diccionarios (claves de COLUMNS + 'profit').
        Devuelve None si el extracto no trae esa hoja."""
        book = book or self._load_book()
        sheet = self._sheet(book, SHEET_NAMES)
        if sheet is None:
            return None
        rows = list(sheet.iter_rows(values_only=True))
        header_index = next((i for i, row in enumerate(rows[:20])
                             if any(_norm(c) in COLUMNS['position_id'] for c in row)), None)
        if header_index is None:
            raise UserError(self.env._('La hoja «%s» no tiene una columna de ID de posición.', sheet.title))
        headers = [_norm(c) for c in rows[header_index]]
        index = {}
        for key, aliases in COLUMNS.items():
            for alias in aliases:
                if alias in headers:
                    index[key] = headers.index(alias)
                    break
        index['profit'] = self._pick(headers, PROFIT_PREFIXES)
        missing = [k for k in ('position_id', 'close_date', 'close_rate') if k not in index]
        if missing or index['profit'] is None:
            raise UserError(self.env._(
                'Faltan columnas en el extracto (%(cols)s). Cabeceras encontradas: %(found)s',
                cols=', '.join(missing + ([] if index['profit'] is not None else ['profit'])),
                found=', '.join(h for h in headers if h)))
        result = []
        for row in rows[header_index + 1:]:
            ref = row[index['position_id']] if index['position_id'] < len(row) else None
            if ref in (None, ''):
                continue
            item = {key: (row[col] if col is not None and col < len(row) else None) for key, col in index.items()}
            item['position_id'] = str(int(ref)) if isinstance(ref, float) else str(ref).strip()
            result.append(item)
        return result

    def _read_dividends(self, book):
        sheet = self._sheet(book, DIVIDEND_SHEETS)
        if sheet is None:
            return []
        headers, index, rows = self._sheet_table(sheet, DIVIDEND_COLUMNS, 'date')
        if headers is None:
            return []
        net_col = self._pick(headers, DIVIDEND_PREFIXES['net'])
        wh_col = self._pick(headers, DIVIDEND_PREFIXES['withholding'])
        if net_col is None:
            raise UserError(self.env._('La hoja «%(sheet)s» no tiene la columna del dividendo neto. Cabeceras: %(found)s',
                                       sheet=sheet.title, found=', '.join(h for h in headers if h)))
        result = []
        for row in rows:
            cell = lambda col: row[col] if col is not None and col < len(row) else None  # noqa: E731
            date = _date(cell(index.get('date')))
            net = _num(cell(net_col))
            if not date or not net:
                continue
            withholding = abs(_num(cell(wh_col)))
            ref = cell(index.get('position_id'))
            result.append({
                'kind': 'dividend', 'date': date.date(), 'name': str(cell(index.get('name')) or '').strip(),
                'isin': str(cell(index.get('isin')) or '').strip() or False,
                'broker_ref': (str(int(ref)) if isinstance(ref, float) else str(ref).strip()) if ref else False,
                'gross': net + withholding, 'withholding': withholding,
            })
        return result

    def _read_interests(self, book):
        sheet = self._sheet(book, ACTIVITY_SHEETS)
        if sheet is None:
            return []
        headers, index, rows = self._sheet_table(sheet, ACTIVITY_COLUMNS, 'type')
        if headers is None or 'amount' not in index or 'date' not in index:
            return []
        result = []
        for row in rows:
            kind = _norm(row[index['type']] if index['type'] < len(row) else '')
            if 'interest' not in kind and 'interes' not in kind:
                continue
            date = _date(row[index['date']])
            amount = _num(row[index['amount']])
            if not date or not amount:
                continue
            details = row[index['details']] if 'details' in index and index['details'] < len(row) else ''
            result.append({
                'kind': 'interest', 'date': date.date(), 'name': str(details or row[index['type']]).strip(),
                'isin': False, 'broker_ref': False, 'gross': amount, 'withholding': 0.0,
            })
        return result

    def _import_incomes(self, items):
        Income = self.env['trading.income']
        created = duplicated = 0
        for item in items:
            domain = [('account_id', '=', self.account_id.id), ('kind', '=', item['kind']), ('date', '=', item['date']),
                      ('broker_ref', '=', item['broker_ref']), ('name', '=', item['name'])]
            if any(abs(i.gross - item['gross']) < 0.005 for i in Income.search(domain)):
                duplicated += 1
                continue
            instrument = self.env['trading.instrument']
            if item['broker_ref']:
                instrument = self.env['trading.position'].with_context(active_test=False).search([
                    ('account_id', '=', self.account_id.id), ('broker_ref', '=', item['broker_ref'])],
                    limit=1).instrument_id
            if not instrument and item['kind'] == 'dividend':
                instrument = self._find_instrument(item['name'])
            Income.create({**item, 'account_id': self.account_id.id, 'instrument_id': instrument.id or False,
                           'name': item['name'] or self.env._('Sin descripción')})
            created += 1
        return created, duplicated

    # ------------------------------------------------------------------
    def _find_instrument(self, action):
        """'Sell EUR/USD' o 'Buy Apple' -> instrumento de la cuenta por símbolo o nombre."""
        Instrument = self.env['trading.instrument']
        text = re.sub(r'^(buy|sell|compra|venta|comprar|vender)\s+', '', str(action or '').strip(), flags=re.I)
        if not text:
            return Instrument
        domain = [('account_id', '=', self.account_id.id)]
        symbol = text.replace('/', '').replace(' ', '').upper()
        found = Instrument.search(domain + [('symbol', '=ilike', symbol)], limit=1)
        if not found:
            found = Instrument.search(domain + [('name', '=ilike', text)], limit=1)
        if not found:
            found = Instrument.search(domain + [('name', 'ilike', text)], limit=2)
            found = found if len(found) == 1 else Instrument
        return found

    def _row_values(self, row):
        profit = _num(row.get('profit'))
        overnight = _num(row.get('overnight'))
        spread = abs(_num(row.get('spread')))
        # El beneficio de eToro ya lleva el spread dentro; las comisiones nocturnas van aparte (negativas)
        # y los dividendos, en la misma columna, en positivo.
        return {
            'state': 'closed',
            'close_date': _date(row.get('close_date')),
            'close_rate': _num(row.get('close_rate')),
            'realized_pnl': profit + overnight,
            'fees': spread + max(0.0, -overnight),
            'close_estimated': False,
            'broker_confirmed': True,
        }

    def action_import(self):
        self.ensure_one()
        Position = self.env['trading.position'].with_context(active_test=False)
        book = self._load_book()
        rows = self._read_rows(book)
        dividends = self._read_dividends(book)
        interests = self._read_interests(book)
        if rows is None and not dividends and not interests:
            names = ', '.join(ws.title for ws in book.worksheets)
            raise UserError(self.env._(
                'No encuentro posiciones cerradas, dividendos ni intereses en el fichero. Hojas: %s', names))
        rows = rows or []
        updated, created, unchanged, skipped, posted = [], [], 0, [], []
        for row in rows:
            vals = self._row_values(row)
            if not vals['close_date']:
                skipped.append(self.env._('%s: fecha de cierre no válida', row['position_id']))
                continue
            position = Position.search([('account_id', '=', self.account_id.id),
                                        ('broker_ref', '=', row['position_id'])], limit=1)
            if position:
                if position.broker_confirmed and position.state == 'closed' and \
                        abs(position.realized_pnl - vals['realized_pnl']) < 0.005:
                    unchanged += 1
                    continue
                before = position.realized_pnl if position.state == 'closed' else None
                position.write(vals)
                position.message_post(body=self.env._(
                    'Cierre confirmado con el extracto de eToro: precio %(rate)s, resultado %(pnl)s (antes %(before)s).',
                    rate=vals['close_rate'], pnl=round(vals['realized_pnl'], 2),
                    before='—' if before is None else round(before, 2)))
                updated.append(position.name)
                if position.move_id:
                    posted.append(position.name)
                continue
            instrument = self._find_instrument(row.get('action'))
            if not instrument:
                skipped.append(self.env._('%(ref)s (%(action)s): instrumento no encontrado en Odoo',
                                          ref=row['position_id'], action=row.get('action') or '?'))
                continue
            side_text = _norm(row.get('side'))
            action_text = _norm(row.get('action'))
            sell = side_text in ('short', 'corto') or action_text.startswith(('sell', 'venta', 'vender'))
            position = Position.create({
                **vals,
                'account_id': self.account_id.id,
                'instrument_id': instrument.id,
                'broker_ref': row['position_id'],
                'side': 'sell' if sell else 'buy',
                'open_date': _date(row.get('open_date')) or vals['close_date'],
                'open_rate': _num(row.get('open_rate')),
                'units': _num(row.get('units')),
                'amount': _num(row.get('amount')),
                'leverage': int(_num(row.get('leverage')) or 1),
            })
            position.message_post(body=self.env._('Creada desde el extracto de eToro.'))
            created.append(position.name)
        div_created, div_dup = self._import_incomes(dividends)
        int_created, int_dup = self._import_incomes(interests)
        lines = [
            self.env._('Posiciones cerradas leídas: %s', len(rows)),
            self.env._('Actualizadas con el cierre real: %s', len(updated)),
            self.env._('Creadas (no estaban en Odoo): %s', len(created)),
            self.env._('Ya confirmadas, sin cambios: %s', unchanged),
            self.env._('Omitidas: %s', len(skipped)),
            self.env._('Dividendos nuevos: %(new)s (ya importados: %(dup)s)', new=div_created, dup=div_dup),
            self.env._('Intereses nuevos: %(new)s (ya importados: %(dup)s)', new=int_created, dup=int_dup),
        ]
        if posted:
            lines += ['', self.env._('Ya tenían asiento contable; revíselo, el resultado ha cambiado:')]
            lines += [f'· {name}' for name in posted]
        if skipped:
            lines += ['', self.env._('Omitidas:')] + [f'· {s}' for s in skipped[:50]]
        self.write({'state': 'done', 'result': '\n'.join(lines)})
        return {
            'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'view_mode': 'form', 'target': 'new', 'name': self.env._('Importar extracto de eToro'),
        }

    @api.model
    def action_open_confirmed(self):
        action = self.env['ir.actions.act_window']._for_xml_id('trading_base.action_trading_position')
        action['context'] = {'search_default_confirmed': 1}
        return action
