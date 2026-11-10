{
    'name': 'Trading — eToro',
    'version': '19.0.1.0.0',
    'summary': 'Conector de eToro (API pública) para Trading',
    'category': 'Trading',
    'author': 'Raúl',
    'license': 'LGPL-3',
    'depends': ['trading_base'],
    'external_dependencies': {'python': ['requests', 'openpyxl']},
    'data': [
        'security/ir.model.access.csv',
        'views/broker_account_views.xml',
        'wizards/statement_import_views.xml',
    ],
    'installable': True,
}
