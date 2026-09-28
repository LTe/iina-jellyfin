"""Hair cards for the drawn wisps the solid hair volume cannot hold.

For each main view, hair pixels that fall outside the model's silhouette are
loose strands. Each connected group becomes a flat alpha-textured quad in that
view's plane through the head, textured straight from the drawing. This is the
usual game technique for flyaway hair.
"""
import os

import numpy as np
from PIL import Image
from scipy import ndimage

import reference as R

PLANES = {"front": -0.012, "back": 0.030, "side": -0.057, "side_mirror": -0.093}   # card plane depth along each view axis (sides sit beside the face)


def cards(views, model_tris, out_dir, head_center, z_min=1.30, min_px=25):
    """Returns [(name, verts, faces, uvs, image_path)] one entry per view."""
    res = []
    for k, off in PLANES.items():
        v = views[k]
        model = R.rasterize_mask(v, model_tris)
        hair = R.classify(v) == R.HAIR
        rows = np.arange(v.h)[:, None] * np.ones((1, v.w))
        z = (v.floor - rows) * v.s
        resid = hair & ~ndimage.binary_dilation(model, iterations=1) & (z > z_min)
        comp, n = ndimage.label(resid, structure=np.ones((3, 3)))
        if n == 0:
            continue
        sizes = ndimage.sum(np.ones_like(comp), comp, range(1, n + 1))
        keep = np.isin(comp, np.where(sizes >= min_px)[0] + 1)
        alpha = ndimage.binary_dilation(keep, iterations=1)
        rgba = v.rgba.copy()
        rgba[:, :, 3] = np.where(alpha, np.maximum(v.rgba[:, :, 3], 200), 0).astype(np.uint8)
        path = os.path.join(out_dir, f"cards_{k}.png")
        Image.fromarray(rgba).save(path)
        # one quad per component, in the plane through the head along the view axis
        d = np.asarray(v.d, float)
        Rr = np.asarray(v.R, float)
        depth = float(np.dot(head_center, d)) + off
        verts, faces, uvs = [], [], []
        objs = ndimage.find_objects(np.where(keep, comp, 0))
        for sl in objs:
            if sl is None:
                continue
            y0, y1 = sl[0].start - 1, sl[0].stop + 1
            x0, x1 = sl[1].start - 1, sl[1].stop + 1
            base = len(verts)
            for (u, vv) in ((x0, y1), (x1, y1), (x1, y0), (x0, y0)):
                P = Rr * ((u - v.u0) * v.s) + np.array([0, 0, (v.floor - vv) * v.s]) + d * depth
                verts.append(tuple(P))
                uvs.append((u / v.w, 1 - vv / v.h))
            faces.append((base, base + 1, base + 2, base + 3))
        res.append((k, verts, faces, uvs, path, int(keep.sum())))
    return res
