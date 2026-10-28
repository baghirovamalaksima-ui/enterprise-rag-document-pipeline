"""Generate local secrets once, then start the API. Never commit .env."""
import argparse
import json
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--init-only', action='store_true')
args = parser.parse_args()
env = ROOT / '.env'
if not env.exists():
    env.write_text('TENANT_KEYS=' + json.dumps({secrets.token_urlsafe(32): 'demo'}) + '\n', encoding='utf-8')
    try:
        env.chmod(0o600)
    except OSError:
        pass
for line in env.read_text(encoding='utf-8').splitlines():
    if line.strip() and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip())
if not args.init_only:
    import sys
    sys.path.insert(0, str(ROOT))
    import uvicorn
    os.chdir(ROOT)
    uvicorn.run('app.main:create_app', factory=True, host='127.0.0.1', port=8000, access_log=False)
