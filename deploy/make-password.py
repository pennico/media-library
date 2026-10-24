#!/usr/bin/env python3
"""Write the console password file: an scrypt hash, never the password.

    python3 deploy/make-password.py <file>

Asks twice (12+ characters). The file format is the agent console's, so the
same file can unlock both."""
import getpass
import hashlib
import os
import secrets
import sys

if len(sys.argv) != 2:
    sys.exit(__doc__)
pw = getpass.getpass("Console password: ")
if len(pw) < 12 or getpass.getpass("Again: ") != pw:
    sys.exit("too short or the two entries differ; nothing written")
salt = secrets.token_bytes(16)
h = hashlib.scrypt(pw.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    fh.write("scrypt$%d$%d$%d$%s$%s\n" % (2**15, 8, 1, salt.hex(), h.hex()))
print("written:", sys.argv[1])
