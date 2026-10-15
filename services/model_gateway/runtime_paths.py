"""Runtime state is configured independently of source location."""
from xtrastock.config import Settings
settings = Settings()
APP = settings.path('paths.app')
DEPLOY_ROOT = settings.root
RUNTIME_STATE = settings.path('paths.state') / 'gateway'
