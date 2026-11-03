"""Test/evaluation-only adapter: real login, workspace URL prefix, no auth bypass."""
import json
from pathlib import Path
import secrets
from app import auth, migration


def bootstrap_database(path):
    migration.initialize_empty_v1(path)
    password = secrets.token_urlsafe(24)
    owner = auth.create_owner(path, 'test-owner', 'Test owner', password)
    return owner, password


class WorkspaceClient:
    def __init__(self, client, workspace_id, csrf):
        self.raw, self.workspace_id, self.csrf = client, workspace_id, csrf
        self.raw.headers['X-CSRF-Token'] = csrf
    def __getattr__(self, name):
        return getattr(self.raw, name)
    @property
    def scope(self):
        return self.raw.app.state.runtime.store(self.workspace_id, self.raw.cookies.get(auth.COOKIE), self.csrf)
    def request(self, method, path, **kwargs):
        if method in ('POST','PUT','PATCH') and not any(k in kwargs for k in ('json','content','data')):
            kwargs['json'] = {}
        if path == '/api/health':
            path = '/api/workspaces/' + self.workspace_id + '/summary'
        elif any(path == '/api/'+name or path.startswith('/api/'+name+'/')
                 for name in ('documents','tasks','search','passages')):
            path = '/api/workspaces/' + self.workspace_id + path[4:]
        return self.raw.request(method, path, **kwargs)
    def get(self, path, **kwargs):return self.request('GET', path, **kwargs)
    def post(self, path, **kwargs):return self.request('POST', path, **kwargs)
    def delete(self, path, **kwargs):return self.request('DELETE', path, **kwargs)


def authenticated(client, owner, password, *, registry=None):
    response = client.post('/api/auth/login', json={'username':'test-owner','password':password})
    assert response.status_code == 200, 'Real login required'
    csrf = response.json()['csrf_token']
    if registry:
        path=Path(registry)
        values=json.loads(path.read_text()) if path.exists() else []
        values.extend([password,csrf,client.cookies.get(auth.COOKIE)])
        path.write_text(json.dumps(values),encoding='utf-8')
    return WorkspaceClient(client, owner['workspace_id'], csrf)
