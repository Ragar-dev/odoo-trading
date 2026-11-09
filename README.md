# Odoo 19 Trading

Odoo 19 Community propio para gestión de trading: brokers (v1: eToro), localización
española (`l10n_es`, PGC PYMES) y precios casi en tiempo real.

## Arranque

```bash
cp config/odoo.conf.example config/odoo.conf   # y cambie admin_passwd
docker compose up -d db
docker compose run --rm odoo odoo -d trading -i trading_etoro --stop-after-init
docker compose up -d odoo
# http://127.0.0.1:8169  (admin / admin)
```

## Módulos

- `trading_base`: cuentas de broker, instrumentos, velas, posiciones, órdenes, panel en vivo
  (OWL, refresco 1 s), cron de precios (1 min), cartera (15 min) y tipos BCE (diario),
  asiento contable PGC de cada posición cerrada (766 / 666 contra 572).
  Gráfica propia en la posición y en el instrumento (Lightweight Charts 5.2.1, Apache 2.0, en
  `static/lib`): velas de 15 min a 1 semana, dibujos guardados en Odoo (`trading.chart.drawing`),
  marcas de entrada, stop, TP, compra y cierre configurables por usuario. Pestaña aparte con la
  gráfica de TradingView (widget público).
- `trading_etoro`: adaptador de la API pública de eToro (`public-api.etoro.com`),
  cabeceras `x-api-key` / `x-user-key` / `x-request-id`. Entorno demo por defecto;
  las órdenes reales exigen marcar «Permitir órdenes reales».

La plantilla contable se elige con el parámetro de sistema `trading.chart_template`
(por defecto `es_pymes`; para Canarias, `es_canary_pymes`). Solo se carga si la compañía no
tiene plan contable ni asientos.

Las claves de eToro se guardan solo en la base de datos (campos de la cuenta de broker),
nunca en el código ni en la configuración.

## Tests

```bash
docker compose run --rm odoo odoo -d trading_test -i trading_etoro \
  --test-tags /trading_base,/trading_etoro --stop-after-init
```

## Limitaciones v1

- eToro: la ruta de cierre en demo (`/api/v1/trading/execution/demo/market-close-orders/...`)
  es un supuesto no verificado. Sin WebSocket; los precios se consultan por REST.
- Las posiciones que desaparecen de la cartera se cierran con precio **estimado**
  (`close_estimated`), porque el endpoint de P/L solo devuelve las abiertas.
