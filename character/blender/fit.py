"""Silhouette fitting: deform meshes so their outline matches the reference views.

Each iteration rasterises the current model in every calibrated view, finds the
vertices that lie on the model's outer silhouette, and moves them along their
screen-space normal toward the drawn outline (using a signed distance field of
the reference mask). Displacements are diffused over the mesh so neighbouring
surface follows, and anything left without data borrows its nearest neighbour's.
"""
import numpy as np
from mathutils import Vector
from mathutils.kdtree import KDTree
from scipy import ndimage

import reference as R


def sdf(mask):
    """Signed distance in pixels, positive outside the mask."""
    return ndimage.distance_transform_edt(~mask) - ndimage.distance_transform_edt(mask)


def sample(field, uv):
    return ndimage.map_coordinates(field, [uv[:, 1], uv[:, 0]], order=1, mode="nearest")


class FitTarget:
    """A mesh (or a face subset of one) whose vertices may move."""

    def __init__(self, ob, face_mask=None, movable=None, mode="mesh", strength=1.0, group=None):
        self.ob = ob
        self.me = ob.data
        self.face_mask = face_mask          # faces that are visible / rasterised
        self.movable = movable              # per-vertex bool
        self.mode = mode                    # "mesh" diffusion or "space" (hair)
        self.strength = strength
        self.group = group                  # (classes, occluders) for part-aware fitting
        n = len(self.me.vertices)
        self.adj = [[] for _ in range(n)]
        for e in self.me.edges:
            a, b = e.vertices
            self.adj[a].append(b)
            self.adj[b].append(a)

    def coords(self):
        return np.array([v.co[:] for v in self.me.vertices])

    def normals(self):
        self.me.update()
        return np.array([v.normal[:] for v in self.me.vertices])

    def triangles(self):
        self.me.calc_loop_triangles()
        co = self.coords()
        tris = [t for t in self.me.loop_triangles
                if self.face_mask is None or self.face_mask[t.polygon_index]]
        idx = np.array([t.vertices[:] for t in tris])
        return co[idx] if len(idx) else np.zeros((0, 3, 3))


def fit(targets, views, iterations=12, step=0.6, max_px=25, log=print, diffuse=25):
    """Part-aware fit: every target belongs to a group (classes, occluders). A group's own
    silhouette is fitted to the reference pixels of its classes; vertices next to an
    occluding class in the drawing are left alone (no evidence there)."""
    labels = {k: R.classify(v) for k, v in views.items()}
    groups = {}
    for t in targets:
        groups.setdefault(t.group, []).append(t)
    rsdf, occl = {}, {}
    for g in groups:
        classes, occluders = g
        for k, v in views.items():
            rsdf[g, k] = sdf(np.isin(labels[k], classes))
            occl[g, k] = ndimage.binary_dilation(np.isin(labels[k], occluders), iterations=4) if occluders \
                else np.zeros_like(v.mask)
    ious = []
    for it in range(iterations):
        all_tris = np.concatenate([t.triangles() for t in targets])
        ious = []
        for k, v in views.items():
            ious.append(R.iou(R.rasterize_mask(v, all_tris), v.mask))
        log(f"  fit iter {it}: IoU " + " ".join(f"{k[:5]}={x:.3f}" for k, x in zip(views, ious)))
        for g, members in groups.items():
            tris = np.concatenate([t.triangles() for t in members])
            msdf, grads = {}, {}
            for k, v in views.items():
                msdf[k] = sdf(R.rasterize_mask(v, tris))
                grads[k] = np.gradient(ndimage.gaussian_filter(msdf[k], 1.0))
            for t in members:
                co = t.coords()
                disp = np.zeros_like(co)
                wsum = np.zeros(len(co))
                for k, v in views.items():
                    uv = v.project(co)
                    inb = (uv[:, 0] > 1) & (uv[:, 0] < v.w - 2) & (uv[:, 1] > 1) & (uv[:, 1] < v.h - 2)
                    on_sil = np.abs(sample(msdf[k], uv)) < 1.6
                    blocked = sample(occl[g, k].astype(float), uv) > 0.5
                    gu, gv = sample(grads[k][1], uv), sample(grads[k][0], uv)
                    gl = np.hypot(gu, gv)
                    ok = inb & on_sil & ~blocked & (gl > 0.2)
                    gu, gv = gu / np.maximum(gl, 1e-6), gv / np.maximum(gl, 1e-6)
                    out = gu[:, None] * np.asarray(v.R)[None] + (-gv)[:, None] * np.array([0, 0, 1.0])[None]
                    s_ref = np.clip(sample(rsdf[g, k], uv), -max_px, max_px)
                    w = ok.astype(float)
                    disp += (w * (-s_ref) * v.s)[:, None] * out
                    wsum += w
                has = wsum > 1e-6
                if t.movable is not None:
                    has &= t.movable
                d = np.zeros_like(co)
                d[has] = disp[has] / wsum[has, None]
                d = spread(t, co, d, has, diffuse)
                if t.movable is not None:
                    d[~t.movable] = 0
                new = co + d * step * t.strength
                for i, vv in enumerate(t.me.vertices):
                    vv.co = new[i]
    return ious


def spread(t, co, d, has, passes):
    if t.mode == "space":
        # hair: blur displacements in space so strands move together
        kd = KDTree(int(has.sum()))
        idx = np.where(has)[0]
        for j, i in enumerate(idx):
            kd.insert(Vector(co[i]), j)
        kd.balance()
        out = np.zeros_like(d)
        if len(idx) == 0:
            return out
        for i in range(len(co)):
            hits = kd.find_n(Vector(co[i]), 12)
            ws = np.array([np.exp(-(h[2] / 0.012) ** 2) for h in hits])
            ds = np.array([d[idx[h[1]]] for h in hits])
            # fall off to nothing a few cm away from any silhouette evidence
            out[i] = (ws[:, None] * ds).sum(0) / (ws.sum() + 0.05)
        return out
    # mesh diffusion: data vertices anchored, the rest relax to their neighbours
    fixed = has.copy()
    cur = d.copy()
    known = has.copy()
    for _ in range(passes):
        nxt = cur.copy()
        nk = known.copy()
        for i in range(len(co)):
            if fixed[i]:
                continue
            nb = [j for j in t.adj[i] if known[j]]
            if nb:
                nxt[i] = cur[nb].mean(0)
                nk[i] = True
        cur, known = nxt, nk
    # smooth everything a little so the silhouette band doesn't crease
    for _ in range(6):
        sm = cur.copy()
        for i in range(len(co)):
            if t.adj[i]:
                sm[i] = 0.5 * cur[i] + 0.5 * cur[t.adj[i]].mean(0)
        cur = sm
    if (~known).any() and known.any():
        kd = KDTree(int(known.sum()))
        idx = np.where(known)[0]
        for j, i in enumerate(idx):
            kd.insert(Vector(co[i]), j)
        kd.balance()
        for i in np.where(~known)[0]:
            _, j, _ = kd.find(Vector(co[i]))
            cur[i] = cur[idx[j]]
    return cur
