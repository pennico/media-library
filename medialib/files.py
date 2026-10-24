"""File hashes: sha256 and the KOReader partial MD5 that kosync uses."""
import hashlib


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def kosync_partial_md5(path):
    """KOReader util.partialMD5: MD5 over 1 KiB samples at offsets
    bit.lshift(1024, 2*i) for i = -1 .. 10, stopping at the end of the file.
    LuaJIT shifts are 32-bit and use only the low 5 bits of the count, so
    i = -1 is offset 0 (not 256). Same as CWA's implementation. This is the
    document id Readest/KOReader send to /kosync."""
    step = size = 1024
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for i in range(-1, 11):
            offset = (step << ((2 * i) & 0x1F)) & 0xFFFFFFFF
            fh.seek(offset)
            sample = fh.read(size)
            if not sample:
                break
            h.update(sample)
    return h.hexdigest()
