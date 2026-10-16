"""MSM 研究子集下載（Kaggle 匿名逐檔，可續傳，GCS MD5 驗證）。

用法：python download_msm.py <dest_root> [--include-acc] [--include-video]
預設排除 acc.csv（32.6 GB）與 reduced.mp4；順序為表格類 → s0 → s2 → s1。
重跑即續傳：已驗證檔跳過，未完成檔以 Range 接續。
結果寫 <dest_root>/manifest.csv 與 download.log。只用標準庫。
"""
import base64, csv, hashlib, json, os, sys, time, urllib.error, urllib.parse, urllib.request

DS = "manufuturetoday/multi-sensor-for-metal-milling-anomaly"
LIST_URL = f"https://www.kaggle.com/api/v1/datasets/list/{DS}?pageSize=200"
DL_URL = f"https://www.kaggle.com/api/v1/datasets/download/{DS}/"
CHUNK = 1 << 20


def list_files():
    files, tok = [], None
    while True:
        u = LIST_URL + (f"&pageToken={tok}" if tok else "")
        d = json.load(urllib.request.urlopen(u, timeout=60))
        files += d.get("datasetFiles") or []
        tok = d.get("nextPageToken")
        if not tok:
            return [(f["name"], int(f["totalBytes"])) for f in files]


def priority(name):
    if name.endswith("_s0.wav"):
        return 1
    if name.endswith("_s2.wav"):
        return 2
    if name.endswith("_s1.wav"):
        return 3
    if name.endswith(("_acc.csv", ".mp4")):
        return 4
    return 0  # label/mtc/ct/nc/png/xml/xlsx


def md5_header(r):
    # GCS 回兩個 x-goog-hash（crc32c、md5），get() 只拿得到第一個
    for h in r.headers.get_all("x-goog-hash") or []:
        for kv in h.split(","):
            k, _, v = kv.strip().partition("=")
            if k == "md5":
                return v
    return ""


def remote_md5(name):
    req = urllib.request.Request(DL_URL + urllib.parse.quote(name, safe=""),
                                 headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return md5_header(r)
    except Exception:
        return ""


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return base64.b64encode(h.digest()).decode()


def fetch(name, size, dest, log):
    out = os.path.join(dest, "raw", name)
    part, hfile = out + ".part", out + ".md5"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    if os.path.exists(out) and os.path.getsize(out) == size:
        if os.path.exists(hfile):
            return "skip"
        want = remote_md5(name)  # 舊版未存雜湊：補探測驗證
        if not want:
            return "ok-size"
        if md5_of(out) == want:
            open(hfile, "w").write(want)
            return "skip-md5"
        log(f"RECHECK {name}: md5 mismatch; refetching")
        os.remove(out)
    attempt, netfail = 0, 0
    while attempt < 8:  # 只有資料錯誤（HTTP 錯誤、大小、MD5）計次；斷網無限等
        have = os.path.getsize(part) if os.path.exists(part) else 0
        try:
            if have != size:  # 已滿檔就直接驗證，避免 Range 越界 416
                req = urllib.request.Request(DL_URL + urllib.parse.quote(name, safe=""))
                if have:
                    req.add_header("Range", f"bytes={have}-")
                with urllib.request.urlopen(req, timeout=120) as r:
                    if have and r.status != 206:
                        have = 0  # 伺服器不給續傳，從頭來
                    v = md5_header(r)
                    if v:
                        open(hfile, "w").write(v)
                    with open(part, "ab" if have else "wb") as f:
                        for b in iter(lambda: r.read(CHUNK), b""):
                            f.write(b)
        except urllib.error.HTTPError as e:
            attempt += 1
            log(f"RETRY {attempt} {name} at {have}: {e!r}")
            time.sleep(min(60, 5 * attempt))
            continue
        except (urllib.error.URLError, OSError) as e:  # 含 DNS 失敗、逾時、連線中斷
            netfail += 1
            if netfail == 1 or netfail % 20 == 0:
                log(f"NETWAIT {netfail} {name} at {have}: {e!r}")
            time.sleep(min(60, 5 * netfail))
            continue
        netfail = 0
        if os.path.getsize(part) != size:
            attempt += 1
            log(f"RETRY {attempt} {name}: size {os.path.getsize(part)} != {size}")
            if os.path.getsize(part) > size:
                os.remove(part)  # 超出必是壞檔，從頭來
            continue
        want = open(hfile).read().strip() if os.path.exists(hfile) else ""
        if want and md5_of(part) != want:
            attempt += 1
            log(f"BADMD5 {attempt} {name}; restarting")
            os.remove(part)
            continue
        os.replace(part, out)
        return "ok-md5" if want else "ok-size"
    return "FAILED"


def main():
    dest = sys.argv[1]
    skip = []
    if "--include-acc" not in sys.argv:
        skip.append("_acc.csv")
    if "--include-video" not in sys.argv:
        skip.append(".mp4")
    os.makedirs(dest, exist_ok=True)
    logf = open(os.path.join(dest, "download.log"), "a", encoding="utf-8")

    def log(msg):
        line = f"{time.strftime('%F %T')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    while True:  # 開跑時若斷網，等到清單抓得到為止
        try:
            listing = list_files()
            break
        except (urllib.error.URLError, OSError) as e:
            log(f"NETWAIT listing: {e!r}")
            time.sleep(60)
    files = [f for f in listing if not f[0].endswith(tuple(skip))]
    files.sort(key=lambda f: (priority(f[0]), f[0]))
    total = sum(s for _, s in files)
    log(f"START {len(files)} files, {total / 1e9:.2f} GB, skip={skip}")
    done, rows = 0, []
    for i, (name, size) in enumerate(files, 1):
        t0 = time.time()
        st = fetch(name, size, dest, log)
        done += size
        dt = time.time() - t0
        log(f"[{i}/{len(files)}] {st:7} {name} {size / 1e6:.1f}MB "
            f"{size / 1e6 / dt if dt > 0.5 and st != 'skip' else 0:.2f}MB/s "
            f"cum {done / 1e9:.2f}/{total / 1e9:.2f}GB")
        rows.append((name, size, st))
    with open(os.path.join(dest, "manifest.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "bytes", "status"])
        w.writerows(rows)
    bad = [r for r in rows if r[2] == "FAILED"]
    log(f"END ok={len(rows) - len(bad)} failed={len(bad)}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
