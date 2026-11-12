"""Test two independent local services and a stopped Windows endpoint."""
import base64
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from trial import ROOT, read, save


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(directory):
    directory.mkdir(parents=True, exist_ok=False)
    manifest = ROOT / "mobile-replay.json"
    mobile_state = directory / "remote.json"
    windows_state = directory / "windows.json"
    mirror = directory / "mirror.json"
    save(windows_state, {"cards": {}, "processed": {}})
    env = dict(os.environ, TODO_PHONE_PASSWORD="test-phone-password", TODO_PUBLISH_TOKEN="test-publish-secret")
    auth = "Basic " + base64.b64encode(b"todo:test-phone-password").decode()
    mobile_port, windows_port = port(), port()
    origin = f"http://127.0.0.1:{mobile_port}"
    windows = f"http://127.0.0.1:{windows_port}"
    py = [sys.executable, "-B"]
    sync = [*py, str(ROOT / "mobile_sync.py")]
    page = [*py, str(ROOT / "todo_server.py")]
    processes = []

    def start(command):
        proc = subprocess.Popen(command, env=env)
        processes.append(proc)
        return proc

    def request(url, data=None):
        headers = {"Authorization": auth}
        if data is not None:
            headers.update({"Content-Type": "application/json", "Origin": origin})
        with urlopen(Request(url, data=json.dumps(data).encode() if data else None, headers=headers), timeout=2) as response:
            return response.read()

    def ready(url):
        for _ in range(30):
            try:
                return request(url)
            except OSError:
                time.sleep(.1)
        raise AssertionError(f"Service did not respond: {url}")

    def client(command, *flags):
        subprocess.run([*sync, command, "--url", origin, *flags], env=env, check=True)

    try:
        remote_command = [*sync, "serve", "--port", str(mobile_port), "--state", str(mobile_state)]
        remote = start(remote_command)
        local = start([*page, "--port", str(windows_port), "--state", str(windows_state)])
        assert b"/api/cards" in ready(origin)
        with urlopen(origin + "/healthz", timeout=2) as response:
            assert response.status == 200
        for path in ("/", "/api/cards", "/api/publish"):
            try:
                urlopen(origin + path, timeout=2)
            except HTTPError as error:
                assert error.code == 401
            else:
                raise AssertionError(f"Unauthenticated access: {path}")
        ready(windows)
        client("publish", "--manifest", str(manifest))
        local.terminate()
        local.wait(timeout=5)
        assert len(json.loads(request(origin + "/api/cards"))) == 3
        request(origin + "/api/status", {"id": "demo-evaluation", "status": "done"})
        request(origin + "/api/status", {"id": "demo-form", "status": "dismissed"})
        remote.terminate()
        remote.wait(timeout=5)
        start(remote_command)
        ready(origin)
        client("publish", "--manifest", str(manifest))
        updated = read(manifest)
        updated["revision"] = 2
        updated["cards"][1]["notes"] = "入口已更新"
        update_file = directory / "updated.json"
        save(update_file, updated)
        client("publish", "--manifest", str(update_file))
        client("publish", "--manifest", str(manifest))
        client("pull", "--state", str(mirror))
        start([*page, "--port", str(windows_port), "--state", str(mirror)])
        ready(windows)
        cards = {c["id"]: c for c in json.loads(request(windows + "/api/cards"))}
        assert len(cards) == 3
        assert cards["demo-evaluation"]["manual_status"] == "done"
        assert cards["demo-evaluation"]["notes"] == "入口已更新"
        assert cards["demo-form"]["manual_status"] == "dismissed"
        assert all(not card["evidence"] for card in cards.values())
        print("PASS: Windows stopped, phone writes persisted, stale and repeated publish safe, Windows mirror restored")
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
