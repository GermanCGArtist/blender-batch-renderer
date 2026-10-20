"""Builds the Windows app folder "Blender Batch Renderer\\" (used by build_windows.bat).

Like build_app.command on the Mac: a trimmed build is tried first and started once as a test;
if it does not start, the app is built again with all of Qt included.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

APP = "Blender Batch Renderer"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def build(full: bool) -> bool:
    shutil.rmtree("build", ignore_errors=True)
    shutil.rmtree("dist", ignore_errors=True)
    env = dict(os.environ, BBR_FULL_BUILD="1" if full else "")
    return subprocess.call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--log-level", "WARN",
                            "bbr.spec"], env=env) == 0


def smoke_test() -> bool:
    tmp = tempfile.mkdtemp()
    exe = os.path.join("dist", APP, APP + ".exe")
    out = os.path.join(tmp, "out.txt")
    env = dict(os.environ, BBR_SMOKE_TEST="1", BBR_API_PORT="0", BBR_DATA_DIR=tmp)
    try:
        with open(out, "w") as f:
            p = subprocess.Popen([exe], env=env, stdout=f, stderr=subprocess.STDOUT)
            try:
                p.wait(60)
            except subprocess.TimeoutExpired:
                p.kill()
                return False
        text = open(out, errors="replace").read()
        # a windowed .exe has no console; the smoke test also writes its result into the data folder
        marker = os.path.join(tmp, "smoke_result.txt")
        if os.path.exists(marker):
            text += open(marker, errors="replace").read()
        ok = "SMOKE TEST OK" in text and "Traceback" not in text
        if not ok:
            print("--- app output ---\n" + text[-2000:] + "\n------------------")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


print("Building the trimmed app ...")
if build(False) and smoke_test():
    print("Trimmed build works.")
else:
    print("The trimmed build did not start - building with all of Qt included instead ...")
    if not (build(True) and smoke_test()):
        print("Build failed.")
        sys.exit(1)
dest = os.path.join(ROOT, APP)
shutil.rmtree(dest, ignore_errors=True)
time.sleep(0.5)
shutil.move(os.path.join("dist", APP), dest)
shutil.rmtree("build", ignore_errors=True)
shutil.rmtree("dist", ignore_errors=True)
print(f"\nBuilt: {dest}\\{APP}.exe")
