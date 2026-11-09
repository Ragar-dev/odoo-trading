"""Contrato común de los adaptadores de broker.

Cada broker registra una subclase de :class:`BrokerAdapter` con ``@register_adapter('codigo')``.
Todos los métodos devuelven estructuras normalizadas (dicts de Python) para que los modelos
no conozcan el formato de cada API.
"""
from datetime import datetime, timezone

ADAPTERS = {}


class BrokerError(Exception):
    """Error legible para el usuario devuelto por un adaptador."""


def register_adapter(code):
    def decorator(cls):
        ADAPTERS[code] = cls
        cls.code = code
        return cls
    return decorator


def parse_datetime(value):
    """ISO 8601 (con 'Z' y fracción de hasta 7 dígitos, estilo .NET) → datetime naive UTC."""
    if not value:
        return False
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace('Z', '+00:00')
        if '.' in text:
            head, _, tail = text.partition('.')
            frac = ''
            rest = ''
            for i, ch in enumerate(tail):
                if not ch.isdigit():
                    rest = tail[i:]
                    break
                frac += ch
            text = f"{head}.{frac[:6]}{rest}" if frac else head + rest
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return False
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class BrokerAdapter:
    """Interfaz. ``account`` es un registro ``trading.broker.account``."""

    code = None

    def __init__(self, account):
        self.account = account

    @property
    def is_demo(self):
        return self.account.environment != 'real'

    # --- Conexión -------------------------------------------------------
    def test_connection(self):
        """Devuelve un texto corto de confirmación o lanza BrokerError."""
        raise BrokerError('Este broker no implementa la prueba de conexión.')

    # --- Datos de mercado ----------------------------------------------
    def search_instruments(self, query):
        """→ [{broker_ref, symbol, name, asset_type}]"""
        return []

    def get_instrument_info(self, broker_ref):
        """→ {broker_ref, symbol, name, asset_type} o {}"""
        return {}

    def get_rates(self, refs):
        """→ {broker_ref: {bid, ask, date, quote_type}}"""
        return {}

    def get_candles(self, broker_ref, interval='OneDay', count=200):
        """→ [{date, open, high, low, close, volume}] ordenadas de antigua a reciente."""
        return []

    # --- Cartera y órdenes ---------------------------------------------
    def get_portfolio(self):
        """→ {credit, unrealized_pnl, positions: [{broker_ref, instrument_ref, side, open_date,
        open_rate, amount, units, leverage, stop_loss, take_profit, fees, broker_pnl}]}"""
        return {'credit': 0.0, 'unrealized_pnl': 0.0, 'positions': []}

    def place_order(self, order):
        """→ {broker_order_id, raw}"""
        raise BrokerError('Este broker no admite enviar órdenes.')

    def close_position(self, position, units=None):
        """→ dict con la respuesta del broker."""
        raise BrokerError('Este broker no admite cerrar posiciones.')


@register_adapter('manual')
class ManualAdapter(BrokerAdapter):
    """Cuenta sin conexión: los datos se introducen a mano."""

    def test_connection(self):
        return 'Cuenta manual: no hay conexión que probar.'
