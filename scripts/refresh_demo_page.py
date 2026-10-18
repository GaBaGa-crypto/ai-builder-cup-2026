"""Copy the judges' demo page and its saved replays into an existing viewer bundle (no MP3 re-export).

Usage: python scripts/refresh_demo_page.py --bundle DIR [--replays-src DIR]
Writes <bundle>/demo_page.html and <bundle>/demo_replays/<name>.json (served by viewer_bundle_server.py at /demo).
Also used by proto/scripts/export_viewer_bundle.py.
"""
import argparse
import json
import os
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGE = os.path.join(REPO, "proto", "scripts", "demo_page.html")
# Saved real runs of the deployed agent (harness lives next to the repo, not in git).
DEFAULT_REPLAYS_SRC = os.path.join(os.path.dirname(REPO), "harness", "agent1-phase2", "work")
REPLAYS = {
    "v4_inspect_data_ctwa.json": "inspect_ctwa.json",
    "v4_rpm_cloud_file.json": "rpm_fresh635.json",
    "gexp_q1.json": "bigquery_counts.json",
    "gexp_q4.json": "refuse_label_write.json",
    "v3_pong.json": "pong.json",
}


def copy_demo_assets(bundle, replays_src=DEFAULT_REPLAYS_SRC):
    shutil.copyfile(PAGE, os.path.join(bundle, "demo_page.html"))
    out = os.path.join(bundle, "demo_replays")
    os.makedirs(out, exist_ok=True)
    copied, missing = [], []
    for src, dst in REPLAYS.items():
        p = os.path.join(replays_src, src)
        if not os.path.isfile(p):
            missing.append(src)
            continue
        with open(p, encoding="utf-8") as fh:
            json.load(fh)  # refuse to publish a broken replay
        shutil.copyfile(p, os.path.join(out, dst))
        copied.append(dst)
    return copied, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--replays-src", default=DEFAULT_REPLAYS_SRC)
    a = ap.parse_args()
    if not os.path.isfile(os.path.join(a.bundle, "sessions.json")):
        raise SystemExit(f"not a viewer bundle (no sessions.json): {a.bundle}")
    copied, missing = copy_demo_assets(a.bundle, a.replays_src)
    print(f"demo_page.html + {len(copied)} replays -> {a.bundle}: {', '.join(copied)}")
    if missing:
        print(f"WARNING missing replay sources in {a.replays_src}: {', '.join(missing)}")


if __name__ == "__main__":
    main()
