"""Measure the face in the drawing: side profile, front outline, eye and ear positions."""
import numpy as np
from scipy import ndimage

import reference as R


def runs(row):
    idx = np.where(row)[0]
    if len(idx) == 0:
        return []
    out, s, p = [], idx[0], idx[0]
    for c in idx[1:]:
        if c != p + 1:
            out.append((s, p))
            s = c
        p = c
    out.append((s, p))
    return out


def measure(views=None):
    views = views or R.load_views(include_mirror=False)
    side, front = views["side"], views["front"]
    ls, lf = R.classify(side), R.classify(front)
    skin_s = ndimage.binary_opening(ls == R.SKIN, iterations=1)
    skin_f = ndimage.binary_opening(lf == R.SKIN, iterations=1)
    zs = np.arange(1.30, 1.66, 0.004)
    prof = []          # (z, y_front) : frontmost skin in the side view
    for z in zs:
        r = int(round(side.floor - z / side.s))
        cols = np.where(skin_s[r])[0]
        if len(cols) == 0:
            continue
        u = cols.max()             # image right = -Y = front of the face
        prof.append((z, (side.u0 - u) * side.s))
    prof = np.array(prof)
    width = []         # (z, x_left, x_right): the skin run through the face centre, front view
    cu = int(round(front.u0))
    for z in zs:
        r = int(round(front.floor - z / front.s))
        for a, b in runs(skin_f[r]):
            if a <= cu <= b:
                width.append((z, (a - front.u0) * front.s, (b - front.u0) * front.s))
    width = np.array(width)
    return {"profile": prof, "width": width}


if __name__ == "__main__":
    m = measure()
    for z, y in m["profile"][::3]:
        print(f"profile z={z:.3f} y_front={y:+.3f}")
    for z, a, b in m["width"][::3]:
        print(f"width   z={z:.3f} x=[{a:+.3f},{b:+.3f}]")
