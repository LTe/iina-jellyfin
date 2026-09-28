"""Silhouette comparison of the built model against the reference sheet.

    python3 character/blender/compare.py [out.png]
"""
import os
import sys

import bpy
import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference as R  # noqa: E402

BLEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "export", "character.blend")


def visible_triangles():
    tris = []
    for o in bpy.data.objects:
        if o.type != "MESH" or o.hide_render:
            continue
        me = o.data
        me.calc_loop_triangles()
        co = np.array([v.co[:] for v in me.vertices])
        idx = np.array([t.vertices[:] for t in me.loop_triangles])
        if len(idx):
            tris.append(co[idx])
    return np.concatenate(tris)


def main():
    out = sys.argv[-1] if sys.argv[-1].endswith(".png") else "/tmp/compare.png"
    bpy.ops.wm.open_mainfile(filepath=BLEND)
    tris = visible_triangles()
    views = R.load_views(include_mirror=False)
    panels = []
    for name, v in views.items():
        m = R.rasterize_mask(v, tris)
        print(f"{name:18s} IoU {R.iou(m, v.mask):.3f}")
        panels.append(R.overlay(v, m))
    H = max(p.shape[0] for p in panels)
    sheet = np.concatenate([np.pad(p, ((0, H - p.shape[0]), (0, 0), (0, 0)), constant_values=255) for p in panels], 1)
    Image.fromarray(sheet).save(out)


if __name__ == "__main__":
    main()
