"""Prepare the mounted data directory, then run AMNOX without root privileges."""
import os
import sys
from pathlib import Path


db_path = Path(os.environ.get("AMNOX_DB_PATH", "/data/nila.sqlite3"))
data_dir = db_path.parent
data_dir.mkdir(parents=True, exist_ok=True)

if os.geteuid() == 0:
    # Container hosts may mount storage as root. Chown only the configured database parent,
    # then drop privileges before starting the web application.
    os.chown(data_dir, 10001, 10001)
    for candidate in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
        if candidate.exists():
            os.chown(candidate, 10001, 10001)
    os.setgroups([])
    os.setgid(10001)
    os.setuid(10001)

os.execv(sys.executable, [sys.executable, "-u", "server.py"])
