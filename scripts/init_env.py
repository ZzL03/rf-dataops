"""Create a private local .env with a random password; never overwrite one."""

import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = root / ".env"
template = (root / ".env.example").read_text(encoding="utf-8")
password = secrets.token_urlsafe(24)
with target.open("x", encoding="utf-8", newline="\n") as output:
    output.write(template.replace("local_dev_change_me", password))
print("Created .env with a random local password (file is ignored by Git).")
