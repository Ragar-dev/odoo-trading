{
    'name': 'Trading — eToro',
    'version': '19.0.1.0.0',
    'summary': 'Conector de eToro (API pública) para Trading',
    'category': 'Trading',
    'author': 'Raúl',
    'license': 'LGPL-3',
    'depends': ['trading_base'],
    'external_dependencies': {'python': ['requests']},
    'data': ['views/broker_account_views.xml'],
    'installable': True,
}
