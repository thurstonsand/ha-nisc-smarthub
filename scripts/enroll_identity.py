"""Install the agent-vault identity an orb receives from Amp as fnox's hidden token.

fnox reads ``~/.config/fnox/config.toml`` on every invocation. Writing the
service-account token there, as a secret that is never exported, lets
``fnox exec`` resolve this repository's ``op://agent/...`` references without
the token ever reaching a child process or the shell.
"""

import json
import os
from pathlib import Path
import sys

TOKEN_NAME = "FNOX_HOST_OP_TOKEN"


def main() -> None:
    """Write the supplied token as fnox's hidden secret, once."""
    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise SystemExit("OP_SERVICE_ACCOUNT_TOKEN must be one nonempty value")
    destination = Path.home() / ".config/fnox/config.toml"
    if destination.exists():
        print(f"{destination} already exists; leaving it alone")
        return
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(
            f"[secrets.{TOKEN_NAME}]\ndefault = {json.dumps(token)}\nenv = false\n"
        )
    print(f"agent identity installed at {destination}", file=sys.stderr)


if __name__ == "__main__":
    main()
