# Odoo Trading

Módulos de **Odoo 19 Community** para gestionar el trading personal desde Odoo: conexión con el
broker, posiciones y carteras copiadas, precios casi en tiempo real, gráficas con dibujos y
resultados fiscales para el IRPF con la localización española (PGC PYMES).

| Módulo | Qué hace |
| --- | --- |
| [`trading_base`](trading_base) | Núcleo independiente del broker: cuentas, instrumentos, velas, posiciones, órdenes, panel en vivo, gráficas, contabilidad e informe fiscal. |
| [`trading_etoro`](trading_etoro) | Conector con la API pública de eToro. |

## Funcionalidades

**Operaciones**
- Cuentas de broker en entorno demo o real. Las órdenes reales exigen marcar «Permitir órdenes reales».
- Posiciones abiertas y cerradas, con P/L no realizado, valor actual y P/L realizado en la moneda de la compañía.
- Carteras copiadas (Smart Portfolios / copy trading) con sus posiciones.
- Órdenes de mercado enviadas al broker y cierre de posiciones desde Odoo.

**Mercado**
- Instrumentos con cotización bid/ask/último, variación diaria y velas de 15 minutos a 1 semana.
- Importación de instrumentos buscando en el broker.
- Panel en vivo (OWL) con refresco cada segundo.

**Gráficas** (en la ficha de cada posición e instrumento)
- Gráfica propia con [Lightweight Charts](https://github.com/tradingview/lightweight-charts):
  línea horizontal, línea de tendencia, rectángulo, nota de texto y goma.
- Los dibujos se guardan en Odoo por instrumento y valen para todos los intervalos.
- Marcas automáticas de la posición: entrada, stop, take profit y flechas de compra, venta y
  cierre, con colores, tamaño y visibilidad configurables por usuario.
- El rango visible y el intervalo se recuerdan en el navegador.
- Pestaña con la gráfica de TradingView (widget público). El símbolo se traduce solo desde el del
  broker y se puede corregir a mano.

**Contabilidad y fiscalidad**
- Clase fiscal orientativa por posición (transmisión de valores, derivado / CFD o criptoactivo).
- Informe «Resultados fiscales (IRPF)» por ejercicio y clase fiscal.
- Asiento PGC opcional de cada posición cerrada (766 / 666 contra 572).
- Tipos de cambio diarios del BCE.

**Tareas programadas**
- Precios: cada minuto.
- Sincronización de carteras: cada 15 minutos.
- Tipos del BCE: diaria.

## Requisitos

- Odoo 19.0 Community.
- Módulos de Odoo: `mail`, `account` y `l10n_es`.
- Python: `requests`.

## Instalación

1. Copie las carpetas `trading_base` y `trading_etoro` en una ruta de `addons_path`.
2. Actualice la lista de aplicaciones.
3. Instale **Trading — eToro**, que instala también `trading_base`:

   ```bash
   odoo -d <base> -i trading_etoro --stop-after-init
   ```

El plan contable se elige con el parámetro de sistema `trading.chart_template` (por defecto
`es_pymes`; para Canarias, `es_canary_pymes`). Solo se carga si la compañía no tiene plan contable
ni asientos.

## Configuración de eToro

1. En eToro, genere las claves de la API pública: la *API key* y la *User key*.
2. En Odoo, vaya a **Trading › Configuración › Cuentas de broker** y cree una cuenta de tipo eToro.
3. Pegue las dos claves, elija el entorno (demo o real) y pulse **Probar conexión**.
4. Pulse **Sincronizar cartera** para traer posiciones y carteras copiadas.

Las claves se guardan solo en la base de datos, visibles únicamente para el grupo *Gestor* de
Trading. Nunca van en el código ni en la configuración.

## Tests

```bash
odoo -d trading_test -i trading_etoro --test-tags /trading_base,/trading_etoro --stop-after-init
```

## Limitaciones

- eToro: la ruta de cierre en demo es un supuesto no verificado. No hay WebSocket; los precios se
  consultan por REST.
- Las posiciones que desaparecen de la cartera del broker se cierran con un precio **estimado**
  (marcado como «Cierre estimado»), porque la API solo devuelve las posiciones abiertas.
- La clase fiscal es orientativa. Revise siempre el informe con su asesor antes de presentar la
  declaración.

## Licencia

Los módulos se publican bajo [LGPL-3](https://www.gnu.org/licenses/lgpl-3.0.html).

`trading_base` incluye Lightweight Charts 5.2.1 de TradingView, bajo licencia Apache 2.0
(`trading_base/static/lib/lightweight-charts/LICENSE`).
