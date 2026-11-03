"""在线一致性备份：docker compose exec web python scripts/backup.py"""
import os, sqlite3
from pathlib import Path
from datetime import datetime
data=Path(os.environ.get('DATA_DIR','data'))
target=data/('backup-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.sqlite3')
with sqlite3.connect(data/'practice.db') as source, sqlite3.connect(target) as dest:
    source.backup(dest)
print(target)
