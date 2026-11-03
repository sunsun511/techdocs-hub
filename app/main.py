"""Integrated B2 backend entrypoint. No implicit migration or account creation."""
from pathlib import Path
import re
from fastapi import Request
from fastapi.responses import FileResponse
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles
from .config import Settings
from .models import Models
from .workspace_api import create_workspace_app
from .auth import COOKIE, AuthError


def create_app(settings=None, models=None):
    settings = settings or Settings.from_env()
    models = models if models is not None else Models(settings.model_dir, settings.models_enabled)
    app = create_workspace_app(settings.db_path, settings.origin, models=models,
                               worker_enabled=settings.worker_enabled)
    app.title = 'TechDocs bilingual document platform'
    app.version = '0.1.0'
    app.state.runtime.settings = settings
    app.state.runtime.worker.max_chunks = settings.max_chunks
    # Existing shell only; its old global API calls remain closed.
    web = Path(__file__).with_name('web')
    app.mount('/assets', StaticFiles(directory=web), name='assets')

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(web / 'index.html')

    @app.get('/openapi.json', include_in_schema=False)
    def schema(request: Request):
        if len(re.findall(r'(?:^|;)\s*'+COOKIE+r'=',request.headers.get('cookie','')))>1:
            raise AuthError(401,'Authentication required')
        app.state.auth.me(request.cookies.get(COOKIE))
        return get_openapi(title=app.title, version=app.version, routes=app.routes)

    return app
