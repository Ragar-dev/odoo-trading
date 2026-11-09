import logging

_logger = logging.getLogger(__name__)


def _load_spanish_chart(env):
    company = env.company
    template = env['ir.config_parameter'].sudo().get_param('trading.chart_template', 'es_pymes')
    # `account` carga 'generic_coa' si la compañía no tiene país; se sustituye si no hay asientos.
    if company.chart_template and company.chart_template != 'generic_coa':
        return
    if env['account.move'].search_count([('company_id', '=', company.id)], limit=1):
        _logger.info('Trading: la compañía ya tiene asientos; no se carga el plan contable.')
        return
    eur = env.ref('base.EUR')
    eur.active = True
    company.write({'country_id': env.ref('base.es').id, 'currency_id': eur.id})
    try:
        env['account.chart.template'].try_loading(template, company, install_demo=False)
        _logger.info('Trading: plan contable %s cargado en %s.', template, company.name)
    except Exception:  # noqa: BLE001 — no debe impedir la instalación
        _logger.exception('Trading: no se pudo cargar la plantilla contable %s', template)


def post_init_hook(env):
    """Activa USD y, si la compañía está virgen, carga el PGC español."""
    usd = env.ref('base.USD', raise_if_not_found=False)
    if usd and not usd.active:
        usd.active = True
    # Si `account` dejó programada la carga de un plan (generic_coa) para el final del arranque
    # del registro, se sustituye por la nuestra; si no, se carga ya.
    if hasattr(env.registry, '_auto_install_template'):
        env.registry._auto_install_template = _load_spanish_chart
    else:
        _load_spanish_chart(env)
