"""Updates without rebuilding: check, download, verify, install, start, fall back, go back.
A local stand-in for GitHub serves the app code.  Run:  python3 tests/test_updater.py"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = tempfile.mkdtemp(prefix="bbr_upd_")
os.environ["BBR_DATA_DIR"] = os.path.join(TMP, "appdata")
failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def snapshot(version, shell=1, tamper=None):
    """A 'commit' of the current code with another version number: (zip bytes, tree, init text)."""
    files = {}
    for top in ("bbr", "blender_addon"):
        for r, _d, fns in os.walk(os.path.join(ROOT, top)):
            if "__pycache__" in r:
                continue
            for fn in fns:
                full = os.path.join(r, fn)
                files[os.path.relpath(full, ROOT).replace(os.sep, "/")] = open(full, "rb").read()
    init = files["bbr/__init__.py"].decode()
    init = re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init)
    init = re.sub(r"SHELL_REQUIRED = \d+", f"SHELL_REQUIRED = {shell}", init)
    files["bbr/__init__.py"] = init.encode()
    tree = {"sha": "x", "truncated": False,
            "tree": [{"path": p, "type": "blob", "sha": blob(d)} for p, d in files.items()]}
    if tamper:
        files[tamper] = files[tamper] + b"\n# changed on the way\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for p, d in files.items():
            z.writestr(f"blender-batch-renderer-x/{p}", d)
    return buf.getvalue(), tree, init


COMMITS = {}      # sha -> (zip, tree, init)
BRANCHES = {}     # branch -> sha


def publish(branch, sha, version, **kw):
    COMMITS[sha] = snapshot(version, **kw)
    BRANCHES[branch] = sha


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path
        m = re.match(r"^/api/repos/[^/]+/[^/]+/commits/(\w+)$", p)
        if m and m.group(1) in BRANCHES:
            sha = BRANCHES[m.group(1)]
            return self._send(json.dumps({"sha": sha, "commit": {"message": f"version on {m.group(1)}\n\nmore",
                                                                  "committer": {"date": "2026-10-20T12:00:00Z"}}}).encode())
        m = re.match(r"^/api/repos/[^/]+/[^/]+/git/trees/(\w+)\?recursive=1$", p)
        if m and m.group(1) in COMMITS:
            return self._send(json.dumps(COMMITS[m.group(1)][1]).encode())
        m = re.match(r"^/raw/[^/]+/[^/]+/(\w+)/bbr/__init__.py$", p)
        if m and m.group(1) in COMMITS:
            return self._send(COMMITS[m.group(1)][2].encode(), "text/plain")
        m = re.match(r"^/codeload/[^/]+/[^/]+/zip/(\w+)$", p)
        if m and m.group(1) in COMMITS:
            return self._send(COMMITS[m.group(1)][0], "application/zip")
        self.send_response(404)
        self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
os.environ.update(BBR_UPDATE_API=base + "/api", BBR_UPDATE_RAW=base + "/raw", BBR_UPDATE_CODELOAD=base + "/codeload")
sys.path.insert(0, ROOT)
import bbr_launcher  # noqa: E402
from bbr import updater, __version__  # noqa: E402

S1, S2, S3, S4, S5 = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)


def launch():
    """Start 'the app' the way the built app does and report which code it runs."""
    code = ("import bbr_launcher; bbr_launcher.activate(); import bbr, bbr.updater; "
            "print(bbr.__version__, bbr.__file__); print('NOTE', bbr_launcher.ACTIVE['fallback_note'])")
    env = dict(os.environ, BBR_USE_UPDATES="1")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True).stdout
    return out


print("1) check the stable channel and install a newer version")
publish("release", S1, "9.0.0")
info = updater.check("stable")
check(info["version"] == "9.0.0" and info["newer"] and info["fits"], f"newer stable version found ({info})")
name = updater.install(info)
st = bbr_launcher.read_state()
check(st["active"] == name and st.get("previous") == "", "installed and active; previous = built-in")
inst = bbr_launcher.app_dir() / name
check((inst / ".ok").exists() and (inst / "bbr" / "assets" / "status.html").exists(), "files verified and in place")
out = launch()
check(out.startswith("9.0.0 ") and str(inst) in out, f"the app starts with the installed code ({out.strip()})")
bbr_launcher.mark_started()

print("2) alpha channel")
publish("main", S2, "9.0.1")
a = updater.check("alpha")
check(a["newer"] and updater.label(a).startswith("9.0.1 alpha"), f"alpha version offered and marked ({updater.label(a)})")

print("3) a file changed on the way is refused")
publish("main", S3, "9.0.2", tamper="bbr/runner.py")
try:
    updater.install(updater.check("alpha"))
    check(False, "damaged download refused")
except updater.UpdateError as e:
    check("checksum" in str(e), f"damaged download refused ({e})")
check(bbr_launcher.read_state()["active"] == name, "the installed version stays active")
check(not [p for p in bbr_launcher.app_dir().iterdir() if p.name.startswith("9.0.2")], "nothing left behind")

print("4) a version that needs a newer app is not installed")
publish("main", S4, "10.0.0", shell=bbr_launcher.SHELL_VERSION + 1)
i4 = updater.check("alpha")
check(i4["fits"] is False, "it is recognised as needing a newer app")
try:
    updater.install(i4)
    check(False, "install refused")
except updater.UpdateError as e:
    check("newer app" in str(e), "install refused with a clear message")

print("5) a version that does not start: back to the previous one by itself")
publish("main", S5, "9.1.0")
bad = updater.install(updater.check("alpha"))
# simulate "crashed while starting" twice (mark_started never called)
launch()
launch()
out = launch()
check(out.startswith("9.0.0 "), f"after two failed starts the previous version runs ({out.splitlines()[0]})")
check("did not start properly" in out, "and the app says so")
check(bad in bbr_launcher.read_state().get("broken", []), "the broken version is remembered")

print("6) 'Use previous version' and back")
bbr_launcher.mark_started()
st = bbr_launcher.read_state()
st["previous"] = bad
bbr_launcher.write_state(st)
shutil.rmtree(bbr_launcher.app_dir() / bad / ".ok", ignore_errors=True)
(bbr_launcher.app_dir() / bad / ".ok").write_text("ok")
updater.use_previous()
out = launch()
check(out.startswith("9.1.0 "), "switched to the previous version")
bbr_launcher.mark_started()
updater.use_previous()
out = launch()
check(out.startswith("9.0.0 "), "and back again")

print("7) running from source ignores installed updates")
out = subprocess.run([sys.executable, "-c", "import bbr_launcher; bbr_launcher.activate(); import bbr; print(bbr.__version__)"],
                     cwd=ROOT, env={k: v for k, v in os.environ.items() if k != "BBR_USE_UPDATES"},
                     capture_output=True, text=True).stdout.strip()
check(out == __version__, f"source run uses the source code ({out})")

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL PASSED")
