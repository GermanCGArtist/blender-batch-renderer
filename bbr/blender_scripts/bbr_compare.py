# Runs inside Blender:  blender -b --factory-startup --python bbr_compare.py -- args.json
#
# Compares two renders of the same frame (from two computers). Both are reduced to blocks of
# pixels first, so render noise (which differs between CPU, GPU and GPU makers) averages out and
# only real differences remain: a missing add-on effect, other colour settings, missing textures.
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bpy  # noqa: E402
import bbr_common as C  # noqa: E402

ARGS = C.script_args()
TOL_MEAN = float(ARGS.get("tol_mean", 0.03))     # mean difference, relative to brightness
TOL_BLOCKS = float(ARGS.get("tol_blocks", 0.02))  # share of blocks that differ clearly


def load(path):
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size[0], img.size[1]
    ch = img.channels
    n = w * h * ch
    try:
        import numpy as np
        px = np.empty(n, dtype=np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(h, w, ch)[:, :, :3]
    except ImportError:
        flat = list(img.pixels[:])
        px = [[flat[(y * w + x) * ch:(y * w + x) * ch + 3] for x in range(w)] for y in range(h)]
    return w, h, px


def blocks_np(px, b):
    import numpy as np
    h, w, _ = px.shape
    h2, w2 = (h // b) * b, (w // b) * b
    px = px[:h2, :w2]
    px = px / (1.0 + np.abs(px))            # squeeze bright (HDR) values into 0..1
    return px.reshape(h2 // b, b, w2 // b, b, 3).mean(axis=(1, 3))


def main():
    wa, ha, a = load(ARGS["a"])
    wb, hb, b = load(ARGS["b"])
    if (wa, ha) != (wb, hb):
        return {"ok": False, "reason": "different image size (%dx%d vs %dx%d)" % (wa, ha, wb, hb)}
    b_size = max(1, min(wa, ha) // 128)
    try:
        import numpy as np
        A, B = blocks_np(a, b_size), blocks_np(b, b_size)
        d = np.abs(A - B)
        mean_d = float(d.mean())
        bright = float((A.mean() + B.mean()) / 2) + 1e-4
        bad = float((d.mean(axis=2) > 0.1).mean())
    except ImportError:
        diffs, tot, bad_n, n = 0.0, 0.0, 0, 0
        for y in range(ha):
            for x in range(wa):
                pa = [v / (1 + abs(v)) for v in a[y][x]]
                pb = [v / (1 + abs(v)) for v in b[y][x]]
                dd = sum(abs(p - q) for p, q in zip(pa, pb)) / 3
                diffs += dd
                tot += (sum(pa) + sum(pb)) / 6
                bad_n += dd > 0.1
                n += 1
        mean_d, bright, bad = diffs / n, tot / n + 1e-4, bad_n / n
    rel = mean_d / bright
    ok = rel <= TOL_MEAN and bad <= TOL_BLOCKS
    reason = "" if ok else "its test frame differs from the reference (%.1f %% overall, %.1f %% of the image " \
                           "clearly different)" % (rel * 100, bad * 100)
    return {"ok": ok, "diff": round(rel, 4), "blocks": round(bad, 4), "reason": reason}


try:
    C.out("COMPARE", main())
except Exception:
    C.out("COMPARE", {"ok": None, "reason": "comparison failed: " + traceback.format_exc().splitlines()[-1]})
