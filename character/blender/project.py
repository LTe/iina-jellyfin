"""Paint the model from the reference: bake the drawn views into UV textures.

For every texel we know its 3D position and normal. Each calibrated view
contributes the drawing's colour at that point when
  * the point faces the view,
  * it is the front-most surface there (z-buffer test), and
  * the drawing shows the same kind of thing there (hair on hair, sweater on
    sweater...), which also rejects the ink outline around each region.
Contributions are blended with a sharp soft-max on facing ratio, so each
texel mostly takes its best view and seams stay soft. Texels no view can see
are filled from their neighbours in texture space.
"""
import os

import bpy
import numpy as np
from PIL import Image
from scipy import ndimage

import reference as R


# ---------------------------------------------------------------------------
def smart_uv(ob, name="Paint", margin=0.004):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.hide_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    uv = ob.data.uv_layers.get(name) or ob.data.uv_layers.new(name=name)
    ob.data.uv_layers.active = uv
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=1.15, island_margin=margin, area_weight=0.0, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    # glTF exports the first/active layer; keep only the painted one
    for n in [l.name for l in ob.data.uv_layers]:
        if n != name and not n.startswith("."):
            ob.data.uv_layers.remove(ob.data.uv_layers[n])
    ob.data.uv_layers[name].active_render = True
    return ob.data.uv_layers[name]


def smooth_normals(me, iters=10):
    """Vertex normals relaxed over the mesh: picks the right view despite small surface ripples."""
    me.update()
    nr = np.array([v.normal[:] for v in me.vertices])
    e = np.array([ed.vertices[:] for ed in me.edges])
    if len(e) == 0:
        return nr
    deg = np.bincount(e.ravel(), minlength=len(nr)).astype(float)
    for _ in range(iters):
        acc = nr.copy()
        np.add.at(acc, e[:, 0], nr[e[:, 1]])
        np.add.at(acc, e[:, 1], nr[e[:, 0]])
        nr = acc / (deg + 1)[:, None]
        nr /= np.maximum(np.linalg.norm(nr, axis=1), 1e-9)[:, None]
    return nr


def mesh_arrays(ob, uv_name="Paint"):
    me = ob.data
    me.calc_loop_triangles()
    uvl = me.uv_layers[uv_name].data
    co = np.array([v.co[:] for v in me.vertices])
    nr = smooth_normals(me)
    tri_v = np.array([t.vertices[:] for t in me.loop_triangles])
    tri_l = np.array([t.loops[:] for t in me.loop_triangles])
    uv = np.array([uvl[i].uv[:] for i in range(len(uvl))])
    return co, nr, tri_v, uv[tri_l]


def texels(ob, size, uv_name="Paint"):
    """Rasterise the UV layout: returns texel pixel coords, 3D positions and normals."""
    co, nr, tri_v, tri_uv = mesh_arrays(ob, uv_name)
    px_all, P_all, N_all = [], [], []
    tuv = tri_uv * size - 0.5
    for t in range(len(tri_v)):
        a, b, c = tuv[t]
        x0, y0 = np.floor(np.minimum(np.minimum(a, b), c)).astype(int)
        x1, y1 = np.ceil(np.maximum(np.maximum(a, b), c)).astype(int)
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, size - 1), min(y1, size - 1)
        if x1 < x0 or y1 < y0:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        p = np.stack([xs.ravel(), ys.ravel()], 1).astype(float)
        v0, v1 = b - a, c - a
        den = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(den) < 1e-12:
            continue
        d = p - a
        l1 = (d[:, 0] * v1[1] - v1[0] * d[:, 1]) / den
        l2 = (v0[0] * d[:, 1] - d[:, 0] * v0[1]) / den
        l0 = 1 - l1 - l2
        eps = -0.02
        inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps)
        if not inside.any():
            continue
        L = np.stack([l0, l1, l2], 1)[inside]
        vi = tri_v[t]
        px_all.append(p[inside].astype(int))
        P_all.append(L @ co[vi])
        N_all.append(L @ nr[vi])
    px = np.concatenate(px_all)
    P = np.concatenate(P_all)
    N = np.concatenate(N_all)
    N /= np.maximum(np.linalg.norm(N, axis=1), 1e-9)[:, None]
    return px, P, N


# ---------------------------------------------------------------------------
def zbuffer(view, tris):
    """Depth (along view.d) of the front-most surface per pixel."""
    zb = np.full((view.h, view.w), np.inf)
    P = tris.reshape(-1, 3)
    uv = view.project(P).reshape(-1, 3, 2)
    dz = view.depth(P).reshape(-1, 3)
    for t in range(len(uv)):
        a, b, c = uv[t]
        x0, y0 = np.floor(np.minimum(np.minimum(a, b), c)).astype(int)
        x1, y1 = np.ceil(np.maximum(np.maximum(a, b), c)).astype(int)
        x0, y0 = max(x0, 0), max(y0, 0)
        x1, y1 = min(x1, view.w - 1), min(y1, view.h - 1)
        if x1 < x0 or y1 < y0:
            continue
        xs, ys = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        p = np.stack([xs.ravel(), ys.ravel()], 1).astype(float)
        v0, v1 = b - a, c - a
        den = v0[0] * v1[1] - v1[0] * v0[1]
        if abs(den) < 1e-12:
            continue
        d = p - a
        l1 = (d[:, 0] * v1[1] - v1[0] * d[:, 1]) / den
        l2 = (v0[0] * d[:, 1] - d[:, 0] * v0[1]) / den
        l0 = 1 - l1 - l2
        ins = (l0 >= -1e-3) & (l1 >= -1e-3) & (l2 >= -1e-3)
        if not ins.any():
            continue
        z = l0[ins] * dz[t, 0] + l1[ins] * dz[t, 1] + l2[ins] * dz[t, 2]
        yy, xx = p[ins, 1].astype(int), p[ins, 0].astype(int)
        np.minimum.at(zb, (yy, xx), z)
    return zb


class Painter:
    def __init__(self, views, occluder_tris, view_weights=None, erode=3):
        self.views = views
        self.labels = {k: R.classify(v) for k, v in views.items()}
        self.zb = {k: zbuffer(v, occluder_tris) for k, v in views.items()}
        self.vw = view_weights or {}
        self.bias = {"side": 0.78, "side_mirror": 0.78, "threequarter": 0.9, "back_threequarter": 0.9}
        self.erode = erode
        self._cls_cache = {}

    def class_mask(self, k, classes):
        key = (k, tuple(classes))
        if key not in self._cls_cache:
            m = np.isin(self.labels[k], classes)
            self._cls_cache[key] = ndimage.binary_erosion(m, iterations=self.erode)
        return self._cls_cache[key]

    def paint(self, ob, classes, size=1024, sharp=6.0, fallback=None, uv_name="Paint", bias=None,
              relaxed_classes=None, min_facing=0.3):
        saved = self.bias
        if bias:
            self.bias = dict(saved, **bias)
        self.min_facing = min_facing
        self.relaxed = relaxed_classes or classes
        try:
            return self._paint(ob, classes, size, sharp, fallback, uv_name)
        finally:
            self.bias = saved

    def _paint(self, ob, classes, size, sharp, fallback, uv_name):
        px, P, N = texels(ob, size, uv_name)
        acc = np.zeros((len(P), 3))
        wsum = np.zeros(len(P))
        self._accumulate(P, N, classes, sharp, acc, wsum, depth_test=True, only=None)
        # second pass for texels no view could see cleanly (e.g. just under a lock of
        # hair): same class rules, no depth test, so they get plausible drawn colour
        miss = wsum <= 0
        if miss.any():
            self._accumulate(P, N, self.relaxed, sharp, acc, wsum, depth_test=False, only=miss)
        return self._finish(ob, px, acc, wsum, size, fallback)

    def _accumulate(self, P, N, classes, sharp, acc, wsum, depth_test, only):
        for k, v in self.views.items():
            facing = N @ (-np.asarray(v.d))
            uv = v.project(P)
            ok = (facing > self.min_facing) & (uv[:, 0] >= 0) & (uv[:, 0] < v.w - 1) & (uv[:, 1] >= 0) & (uv[:, 1] < v.h - 1)
            ui, vi = np.clip(np.round(uv[:, 0]).astype(int), 0, v.w - 1), np.clip(np.round(uv[:, 1]).astype(int), 0, v.h - 1)
            ok &= self.class_mask(k, classes)[vi, ui]
            if depth_test:
                ok &= v.depth(P) <= self.zb[k][vi, ui] + 0.012
            if only is not None:
                ok &= only
            if not ok.any():
                continue
            rgb = v.rgba[:, :, :3].astype(float)
            col = np.stack([ndimage.map_coordinates(rgb[:, :, c], [uv[ok, 1], uv[ok, 0]], order=1) for c in range(3)], 1)
            # bias: a view's facing ratio is scaled before the soft-max, so side
            # views only win on surfaces clearly turned toward them
            w = (np.clip(facing[ok] * self.bias.get(k, 1.0), 0, None) ** sharp) * self.vw.get(k, 1.0)
            acc[ok] += col * w[:, None]
            wsum[ok] += w

    def _finish(self, ob, px, acc, wsum, size, fallback):
        img = np.zeros((size, size, 3))
        wimg = np.zeros((size, size))
        good = wsum > 0
        img[px[good, 1], px[good, 0]] = acc[good] / wsum[good, None]
        wimg[px[good, 1], px[good, 0]] = 1
        covered = np.zeros((size, size), bool)
        covered[px[:, 1], px[:, 0]] = True
        if fallback is None:
            fallback = np.median(img[wimg > 0], 0) if (wimg > 0).any() else np.array([200, 160, 140.0])
        if os.environ.get("PAINT_DEBUG"):
            dbg = np.zeros((size, size, 3), np.uint8)
            dbg[covered] = (90, 90, 90)
            dbg[wimg > 0] = (0, 200, 0)
            Image.fromarray(dbg[::-1]).save(f"/tmp/cov_{ob.name}.png")
        img = fill(img, wimg, covered, fallback)
        frac = good.mean()
        return img, frac


def fill(img, known, covered, fallback, iters=6):
    """Push colours from painted texels into unpainted ones (and a margin around islands)."""
    img = img.copy()
    k = known.astype(float)
    target = ndimage.binary_dilation(covered, iterations=4)
    for _ in range(iters):
        if (k[target] > 0).all():
            break
        s = ndimage.uniform_filter(img * k[..., None], (3, 3, 1)) * 9
        c = ndimage.uniform_filter(k, 3) * 9
        grow = (c > 0.5) & (k == 0)
        img[grow] = s[grow] / c[grow, None]
        k[grow] = 1
    rest = target & (k == 0)
    img[rest] = fallback
    # soften the edge between painted texels and the flat fallback
    blur = ndimage.gaussian_filter(img, (2, 2, 0))
    band = ndimage.binary_dilation(rest, iterations=3) & target & ~known.astype(bool)
    img[band] = blur[band]
    return img


def assign_texture(ob, img, name, out_dir):
    path = os.path.join(out_dir, f"{name}.jpg")
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)[::-1]).save(path, quality=92)
    im = bpy.data.images.load(path)
    im.name = name
    im.pack()
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = im
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Emission Color"])
    bsdf.inputs["Emission Strength"].default_value = 0.0
    bsdf.inputs["Roughness"].default_value = 0.9
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.1
    m["painted"] = True
    ob.data.materials.clear()
    ob.data.materials.append(m)
    for p in ob.data.polygons:
        p.material_index = 0
    return m


def carve(ob, painter, kill_classes, views=("front", "side", "side_mirror", "back", "threequarter"), erode=1, eps=0.004):
    """Delete faces that are front-most in a view where the drawing shows something else
    (e.g. hair geometry hanging over what the artist drew as forehead)."""
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    C = np.array([f.calc_center_median()[:] for f in bm.faces])
    Nn = np.array([f.normal[:] for f in bm.faces])
    kill = np.zeros(len(C), bool)
    for k in views:
        v = painter.views[k]
        m = np.isin(painter.labels[k], kill_classes)
        m = ndimage.binary_erosion(m, iterations=erode)
        uv = v.project(C)
        ui = np.clip(np.round(uv[:, 0]).astype(int), 0, v.w - 1)
        vi = np.clip(np.round(uv[:, 1]).astype(int), 0, v.h - 1)
        front = v.depth(C) <= painter.zb[k][vi, ui] + eps
        kill |= front & m[vi, ui]
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.where(kill)[0]], context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()
    return int(kill.sum())


def grow(src, painter, classes, views=("front", "side", "side_mirror", "back"), offset=0.003, erode=1, eps=0.004,
         name="HairGrown"):
    """Counterpart of carve: copy the faces of `src` that are front-most where the drawing
    shows `classes` (e.g. head skin under drawn hair) and lift them off the surface, as a new mesh."""
    import bmesh
    me = src.data
    me.update()
    C = np.array([p.center[:] for p in me.polygons])
    Nn = np.array([p.normal[:] for p in me.polygons])
    keep = np.zeros(len(C), bool)
    for k in views:
        v = painter.views[k]
        m = ndimage.binary_erosion(np.isin(painter.labels[k], classes), iterations=erode)
        uv = v.project(C)
        ui = np.clip(np.round(uv[:, 0]).astype(int), 0, v.w - 1)
        vi = np.clip(np.round(uv[:, 1]).astype(int), 0, v.h - 1)
        front = v.depth(C) <= painter.zb[k][vi, ui] + eps
        keep |= front & m[vi, ui] & ((Nn @ (-np.asarray(v.d))) > 0.15)
    if not keep.any():
        return None
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in np.where(~keep)[0]], context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bm.normal_update()
    for v in bm.verts:
        v.co += v.normal * offset
    nme = bpy.data.meshes.new(name)
    bm.to_mesh(nme)
    bm.free()
    ob = bpy.data.objects.new(name, nme)
    bpy.context.scene.collection.objects.link(ob)
    for p in nme.polygons:
        p.use_smooth = True
    return ob
