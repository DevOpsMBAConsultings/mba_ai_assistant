{
    'name': 'Asistente de IA (MBA Consultings)',
    'version': '18.0.1.2',
    'category': 'Productivity',
    'summary': 'Asistente de Inteligencia Artificial (Claude, Gemini, OpenAI) en la barra superior (Systray) con contexto de vistas.',
    'description': """
        Este módulo integra un asistente de Inteligencia Artificial interactivo en el Systray de la barra superior.
        Permite configurar proveedores como Google Gemini, Anthropic Claude y OpenAI desde los Ajustes Generales del sistema.
    """,
    'author': 'MBA Consultings',
    'website': 'https://www.mbaconsultings.com',
    'depends': ['web', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/mba_ai_interaction_log_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'mba_ai_assistant/static/src/components/ai_assistant_systray.js',
            'mba_ai_assistant/static/src/components/ai_assistant_systray.xml',
            'mba_ai_assistant/static/src/components/ai_assistant_systray.scss',
        ],
    },
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
