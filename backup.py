import os
import sqlite3
import sys
from datetime import datetime

DATABASE = os.getenv("DATABASE_PATH", "/data/medical_diary.db")
stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
dest = sys.argv[1] if len(sys.argv) > 1 else f"/backups/medical_diary_{stamp}.db"

os.makedirs(os.path.dirname(dest), exist_ok=True)

src = sqlite3.connect(DATABASE)
dst = sqlite3.connect(dest)

with dst:
    src.backup(dst)

src.close()
dst.close()

print(dest)
