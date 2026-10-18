"""HTTP helpers shared by the local viewer module and the Cloud Run bundle server (standard library only)."""
import os
import re

CHUNK = 1 << 16
MAX_RANGE_HEADER = 128


def _range_error(h, size):
    h.send_response(416)
    h.send_header("Content-Range", f"bytes */{size}")
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", "0")
    h.send_header("Cache-Control", "private, max-age=3600")
    h.end_headers()


def send_bytes(h, body, ctype, status=200, extra=()):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    for k, v in extra:
        h.send_header(k, v)
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def send_file(h, path, ctype):
    """Serve a file with HTTP Range support (206), so <audio> can seek."""
    size = os.path.getsize(path)
    start, end, status = 0, size - 1, 200
    raw_range = h.headers.get("Range", "") or ""
    if len(raw_range) > MAX_RANGE_HEADER:
        return _range_error(h, size)
    rng = re.fullmatch(r"bytes=(\d*)-(\d*)", raw_range)
    if raw_range and (not rng or not (rng.group(1) or rng.group(2))):
        return _range_error(h, size)
    if rng and (rng.group(1) or rng.group(2)):
        if rng.group(1):
            start = int(rng.group(1))
            end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
        else:  # bytes=-N：最後 N 個位元組
            start = max(0, size - int(rng.group(2)))
        if start > end or start >= size:
            return _range_error(h, size)
        status = 206
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(end - start + 1))
    h.send_header("Cache-Control", "private, max-age=3600")
    if status == 206:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.end_headers()
    if h.command == "HEAD":
        return
    with open(path, "rb") as f:
        f.seek(start)
        left = end - start + 1
        try:
            while left > 0:
                b = f.read(min(CHUNK, left))
                if not b:
                    break
                h.wfile.write(b)
                left -= len(b)
        except (BrokenPipeError, ConnectionResetError):  # 瀏覽器跳播時會中斷舊連線
            pass
