"""Solve each reference view's horizontal origin (and the 3/4 azimuths) by
maximising silhouette overlap with the current model. Writes reference/calibration.json."""
import json
import os
import sys

import bpy
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference as R  # noqa: E402
from compare import BLEND, visible_triangles  # noqa: E402


def best_shift(v, m):
    best = (-1, 0)
    for du in range(-40, 41):
        sm = np.roll(m, du, axis=1)
        best = max(best, (R.iou(sm, v.mask), du))
    return best


def main():
    bpy.ops.wm.open_mainfile(filepath=BLEND)
    tris = visible_triangles()
    views = R.load_views(include_mirror=False, calibrated=False)
    cal = {}
    for name, v in views.items():
        azs = [v.az] if name in ("front", "side", "back") else [v.az + d for d in range(-16, 17, 2)]
        best = None
        for az in azs:
            v.set_azimuth(az)
            score, du = best_shift(v, R.rasterize_mask(v, tris))
            if best is None or score > best[0]:
                best = (score, du, az)
        score, du, az = best
        cal[name] = {"u0": float(v.u0 + du), "az": az, "iou": round(score, 4)}
        print(name, cal[name])
    json.dump(cal, open(R.CALIB, "w"), indent=2)


if __name__ == "__main__":
    main()
