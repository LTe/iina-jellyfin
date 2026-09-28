"""Hair volume as a visual hull of the drawing.

A voxel survives if, in every calibrated view, it projects inside the drawn
head-and-hair region (hair or skin pixels). Voxels inside the modelled head or
body are removed, and what is left is the hair: its outline matches the sheet
from every drawn angle. Marching cubes turns it into a smooth mesh.
"""
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree
from scipy import ndimage
from skimage import measure

import reference as R

BOX = ((-0.20, 0.22), (-0.20, 0.26), (1.32, 1.80))


def inside(obj_list, pts):
    """Boolean: point is inside any of the (closed-ish) meshes, via nearest-surface normal test."""
    res = np.zeros(len(pts), bool)
    for ob in obj_list:
        me = ob.data
        bvh = BVHTree.FromPolygons([v.co.copy() for v in me.vertices], [tuple(p.vertices) for p in me.polygons])
        for i, p in enumerate(pts):
            if res[i]:
                continue
            loc, nrm, _, d = bvh.find_nearest(Vector(p))
            if loc is not None and d < 0.08 and (Vector(p) - loc).dot(nrm) < 0:
                res[i] = True
    return res


def solid_tris(objs):
    out = []
    for ob in objs:
        ob.data.calc_loop_triangles()
        co = np.array([v.co[:] for v in ob.data.vertices])
        idx = np.array([t.vertices[:] for t in ob.data.loop_triangles])
        out.append(co[idx])
    return np.concatenate(out)


def build(views, solids, voxel=0.003, views_used=None, dilate=1, log=print, box=BOX,
          classes=(R.HAIR, R.SKIN), keep_largest=False, smooth=1.0, reveal_skin=False):
    views_used = views_used or list(views)
    xs = np.arange(box[0][0], box[0][1], voxel)
    ys = np.arange(box[1][0], box[1][1], voxel)
    zs = np.arange(box[2][0], box[2][1], voxel)
    X, Y, Z = np.meshgrid(xs, ys, zs, indexing="ij")
    P = np.stack([X.ravel(), Y.ravel(), Z.ravel()], 1)
    keep = np.ones(len(P), bool)
    for k in views_used:
        v = views[k]
        lbl = R.classify(v)
        m = np.isin(lbl, classes)
        m = ndimage.binary_dilation(m, iterations=dilate)
        uv = v.project(P[keep])
        ui = np.round(uv[:, 0]).astype(int)
        vi = np.round(uv[:, 1]).astype(int)
        ok = (ui >= 0) & (ui < v.w) & (vi >= 0) & (vi < v.h)
        hit = np.zeros(len(ui), bool)
        hit[ok] = m[vi[ok], ui[ok]]
        idx = np.where(keep)[0]
        keep[idx[~hit]] = False
        log(f"  hull after {k:18s}: {keep.sum():8d} voxels")
    # remove the head and body (neck) from the hull
    if reveal_skin and solids:
        # where the drawing shows skin, nothing may sit in front of the head/neck surface
        import project as PJ
        tris = solid_tris(solids)
        for k in ("front", "side", "side_mirror", "back", "threequarter"):
            v = views[k]
            zb = PJ.zbuffer(v, tris)
            skin = ndimage.binary_erosion(R.classify(v) == R.SKIN, iterations=1)
            idx = np.where(keep)[0]
            uv = v.project(P[idx])
            ui = np.clip(np.round(uv[:, 0]).astype(int), 0, v.w - 1)
            vi = np.clip(np.round(uv[:, 1]).astype(int), 0, v.h - 1)
            infront = v.depth(P[idx]) < zb[vi, ui] - 0.002
            kill = skin[vi, ui] & infront
            keep[idx[kill]] = False
            log(f"  hull reveal skin {k:12s}: -{kill.sum()} voxels")
    if solids:
        idx = np.where(keep)[0]
        ins = inside(solids, P[idx])
        keep[idx[ins]] = False
        log(f"  hull minus solids    : {keep.sum():8d} voxels")
    vol = keep.reshape(X.shape).astype(float)
    # keep the biggest connected piece plus anything sizeable (loose locks)
    lab, n = ndimage.label(vol > 0.5)
    if n > 1:
        sizes = ndimage.sum(np.ones_like(lab), lab, range(1, n + 1))
        good = [int(np.argmax(sizes)) + 1] if keep_largest else np.where(sizes >= max(400, sizes.max() * 0.04))[0] + 1
        vol = np.isin(lab, good).astype(float)
    vol = ndimage.gaussian_filter(vol, smooth)
    vol = np.pad(vol, 1)
    verts, faces, _, _ = measure.marching_cubes(vol, 0.5)
    verts = (verts - 1) * voxel + np.array([xs[0], ys[0], zs[0]])
    return verts, faces
