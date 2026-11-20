"""Private HTTP helper for the snapshot dashboard. Nothing runs on a schedule."""
import json
import os
import subprocess
import sys
import threading

from contextlib import closing
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parents[1]
COOLDOWN = 3 * 3600
STATE = Path(os.environ.get("SNAPSHOT_DIR", "/snapshot"))


def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"], connect_timeout=4)


def initialize():
    with closing(connect()) as db, db, db.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS ops.map_refresh (
            id integer PRIMARY KEY CHECK(id=1), attempted bigint NOT NULL, status text NOT NULL)""")
        cur.execute("INSERT INTO ops.map_refresh VALUES(1,0,'ready') ON CONFLICT DO NOTHING")
        # This singleton worker is the only executor. Its child dies with the container.
        cur.execute("UPDATE ops.map_refresh SET status='failed' WHERE status='running'")


def snapshot_time():
    try:
        data = json.loads((STATE / "mart/outages.json").read_bytes())
        return int(datetime.fromisoformat(data["updatedAt"].replace("Z", "+00:00")).timestamp())
    except (OSError, ValueError, KeyError):
        return 0


def scrape():
    subprocess.run([sys.executable, str(ROOT / "scripts/refresh_snapshot.py")],
                   check=True, timeout=300, cwd=ROOT)


def complete():
    status = "cached"
    try:
        scrape()
    except Exception as exc:
        print(f"Refresh failed: {exc}", file=sys.stderr, flush=True)
        status = "failed"
    finally:
        with closing(connect()) as db, db, db.cursor() as cur:
            cur.execute("UPDATE ops.map_refresh SET status=%s WHERE id=1", (status,))


def refresh(start=False):
    with closing(connect()) as db, db, db.cursor() as cur:
        sql = "SELECT attempted,status,extract(epoch from clock_timestamp())::bigint FROM ops.map_refresh WHERE id=1"
        cur.execute(sql + (" FOR UPDATE" if start else ""))
        attempted, status, now = cur.fetchone()
        # If the database was unavailable when completion was recorded, a lost
        # worker must not leave the button stuck forever. Scrapes time out at 5m.
        if status == "running" and now - attempted > 360:
            status = "failed"
        eligible = max(snapshot_time(), attempted) + COOLDOWN
        if status != "running":
            if now < eligible:
                status = "failed" if status == "failed" else "cached"
            elif start:
                cur.execute("UPDATE ops.map_refresh SET attempted=%s,status='running' WHERE id=1", (now,))
                db.commit()
                threading.Thread(target=complete, daemon=True).start()
                status, eligible = "running", now + COOLDOWN
            else:
                status = "ready"
    return {"status": status, "nextAllowedAt": eligible}


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self):
        try:
            if self.path == "/refresh":
                result = refresh(self.command == "POST")
                return self.reply(202 if result["status"] == "running" else 200, result)
            self.reply(404, {"error": "not found"})
        except (TypeError, ValueError) as exc:
            self.reply(400, {"error": str(exc)})
        except psycopg2.Error as exc:
            print(f"Local database error: {exc}", file=sys.stderr, flush=True)
            self.reply(503, {"error": "Database unavailable"})

    do_GET = dispatch
    do_POST = dispatch

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    initialize()
    with ThreadingHTTPServer(("0.0.0.0", 8091), Handler) as server:
        while True:
            server.handle_request()
