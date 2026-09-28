"""Procedural build of the "cozy pajamas" character from reference/turnaround.png.

Run with the Blender Python module (pip install bpy) or inside Blender:

    python3 character/blender/build_character.py            # build + export + renders
    python3 character/blender/build_character.py --no-render

Everything is generated from the parameters below so the model can be iterated
by tweaking numbers and re-running. Units are meters, Z up, character faces -Y
(which becomes +Z forward in glTF).
"""
import math
import os
import random
import sys

import numpy as np

import bpy
import bmesh
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_EXPORT = os.path.join(ROOT, "export")
OUT_RENDER = os.path.join(ROOT, "renders")
OUT_TEX = os.path.join(ROOT, "textures")
for d in (OUT_EXPORT, OUT_RENDER, OUT_TEX):
    os.makedirs(d, exist_ok=True)

RNG = random.Random(7)

# --------------------------------------------------------------------------
# Palette (sRGB hex, sampled from the reference sheet)
# --------------------------------------------------------------------------
COL = {
    "skin": "#E3A47C",
    "skin_shadow": "#C98362",
    "shirt": "#2F3A6E",
    "pants": "#6F7BAC",
    "pants_line": "#CFCFDF",
    "pants_line2": "#9AA6D6",
    "hair": "#6A3A1F",
    "hair_dark": "#57301A",
    "string": "#ECE4D6",
    "brow": "#4A2A17",
    "iris": "#6B3A1C",
    "lip": "#B8615A",
    "blush": "#EE8C7C",
    "nail": "#EFB79A",
    "underwear": "#D8CFC6",
    "tee": "#CFC8E2",
}


def srgb(hexstr, a=1.0):
    h = hexstr.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return (*lin, a)


def rgb255(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


# --------------------------------------------------------------------------
# Scene helpers
# --------------------------------------------------------------------------
def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def link(obj):
    bpy.context.scene.collection.objects.link(obj)
    return obj


def mesh_obj(name, verts, faces, mat=None, smooth=True):
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], faces)
    me.update()
    ob = link(bpy.data.objects.new(name, me))
    if smooth:
        for p in me.polygons:
            p.use_smooth = True
    if mat:
        me.materials.append(mat)
    return ob


def bake(ob):
    """Apply all modifiers of `ob` in place (without bpy.ops)."""
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    old = ob.data
    ob.modifiers.clear()
    ob.data = me
    me.name = ob.name
    bpy.data.meshes.remove(old)
    return ob


def shade_smooth(ob):
    for p in ob.data.polygons:
        p.use_smooth = True


def material(name, color, rough=0.75, image=None, spec=0.25):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = nt.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = srgb(color) if isinstance(color, str) else color
    bsdf.inputs["Roughness"].default_value = rough
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = spec
    if image is not None:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tex.location = (-400, 200)
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    m.diffuse_color = srgb(color) if isinstance(color, str) else color
    return m


# --------------------------------------------------------------------------
# Skin-modifier "tube skeleton" meshes (clean quad topology)
# --------------------------------------------------------------------------
def skin_mesh(name, nodes, edges, root=0, subdiv=2, mat=None):
    """nodes: list of (co, (rx, ry)). Builds a skin-modifier mesh and bakes it."""
    me = bpy.data.meshes.new(name)
    me.from_pydata([n[0] for n in nodes], edges, [])
    me.update()
    ob = link(bpy.data.objects.new(name, me))
    sk = ob.modifiers.new("Skin", "SKIN")
    sk.use_smooth_shade = True
    sk.branch_smoothing = 0.6
    layer = me.skin_vertices[0].data
    for i, n in enumerate(nodes):
        layer[i].radius = n[1]
        layer[i].use_root = (i == root)
    sd = ob.modifiers.new("Subsurf", "SUBSURF")
    sd.levels = subdiv
    sd.render_levels = subdiv
    bake(ob)
    shade_smooth(ob)
    if mat:
        ob.data.materials.append(mat)
    return ob


def V(x, y, z):
    return Vector((x, y, z))


def mirror_x(v):
    return Vector((-v.x, v.y, v.z))


# --------------------------------------------------------------------------
# Sweep: tube along a path with elliptical cross-section (hair, strings)
# --------------------------------------------------------------------------
def catmull(ctrl, n=8, closed=False):
    pts = [Vector(c) for c in ctrl]
    if closed:
        P = [pts[-1]] + pts + [pts[0], pts[1]]
        segs = len(pts)
    else:
        P = [pts[0] * 2 - pts[1]] + pts + [pts[-1] * 2 - pts[-2]]
        segs = len(pts) - 1
    out = []
    for i in range(segs):
        p0, p1, p2, p3 = P[i], P[i + 1], P[i + 2], P[i + 3]
        for k in range(n):
            t = k / n
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    if not closed:
        out.append(pts[-1].copy())
    return out


def sweep_geo(pts, width, thick, up=None, ring=8, closed=False, cap=True):
    """Return (verts, faces) of a tube. width/thick: callables t->half-size or floats.
    up: callable(i, p) -> vector the 'thick' axis should align with (e.g. surface normal)."""
    n = len(pts)
    wf = width if callable(width) else (lambda t, w=width: w)
    hf = thick if callable(thick) else (lambda t, h=thick: h)
    verts, faces = [], []
    prev_up = None
    for i, p in enumerate(pts):
        if closed:
            T = (pts[(i + 1) % n] - pts[(i - 1) % n])
        else:
            T = pts[min(i + 1, n - 1)] - pts[max(i - 1, 0)]
        T.normalize()
        if up is not None:
            U = Vector(up(i, p))
        elif prev_up is not None:
            U = prev_up
        else:
            U = Vector((0, 0, 1)) if abs(T.z) < 0.9 else Vector((1, 0, 0))
        B = T.cross(U)
        if B.length < 1e-6:
            B = T.orthogonal()
        B.normalize()
        N = B.cross(T).normalized()
        prev_up = N
        t = i / (n - 1 if not closed else n)
        w, h = wf(t), hf(t)
        for k in range(ring):
            a = 2 * math.pi * k / ring
            verts.append(p + B * (w * math.cos(a)) + N * (h * math.sin(a)))
    segs = n if closed else n - 1
    for i in range(segs):
        i2 = (i + 1) % n
        for k in range(ring):
            k2 = (k + 1) % ring
            faces.append((i * ring + k, i * ring + k2, i2 * ring + k2, i2 * ring + k))
    if cap and not closed:
        for idx, pt in ((0, pts[0]), (n - 1, pts[-1])):
            verts.append(pt.copy())
            c = len(verts) - 1
            for k in range(ring):
                k2 = (k + 1) % ring
                a, b = idx * ring + k, idx * ring + k2
                faces.append((a, b, c) if idx == n - 1 else (b, a, c))
    return verts, faces


class GeoBatch:
    """Accumulates many swept pieces into one mesh object."""

    def __init__(self):
        self.v, self.f = [], []

    def add(self, verts, faces):
        o = len(self.v)
        self.v.extend(verts)
        self.f.extend(tuple(i + o for i in f) for f in faces)

    def build(self, name, mat):
        return mesh_obj(name, self.v, self.f, mat)


# --------------------------------------------------------------------------
# Textures (generated with numpy/PIL so everything stays reproducible)
# --------------------------------------------------------------------------
def save_image(path, pil_img, name):
    pil_img.save(path)
    img = bpy.data.images.load(path)
    img.name = name
    img.pack()
    return img


def plaid_texture(size=1024):
    import numpy as np
    from PIL import Image
    base = np.array(rgb255(COL["pants"]), dtype=float)
    line = np.array(rgb255(COL["pants_line"]), dtype=float)
    line2 = np.array(rgb255(COL["pants_line2"]), dtype=float)
    img = np.tile(base, (size, size, 1))
    yy, xx = np.mgrid[0:size, 0:size] / size
    rng = np.random.default_rng(3)
    # fabric noise
    img *= (0.96 + 0.05 * rng.random((size, size)))[..., None]
    # tile: 4 checks per texture; each check has a thin bright line + a faint secondary line
    rep = 4

    def stripe(c, pos, w):
        d = np.abs(((c * rep - pos) + 0.5) % 1.0 - 0.5)
        return np.clip(1.0 - d / w, 0, 1) ** 0.7

    s1 = np.maximum(stripe(xx, 0.0, 0.022), stripe(yy, 0.0, 0.022)) * 0.75
    s2 = np.maximum(stripe(xx, 0.5, 0.012), stripe(yy, 0.5, 0.012)) * 0.35
    img = img * (1 - s2[..., None]) + line2 * s2[..., None]
    img = img * (1 - s1[..., None] * 0.85) + line * (s1[..., None] * 0.85)
    im = Image.fromarray(np.clip(img, 0, 255).astype("uint8"), "RGB")
    return save_image(os.path.join(OUT_TEX, "pants_plaid.png"), im, "pants_plaid")


# Face texture uses a front planar projection: u = (x-FX0)/FW, v = (z-FZ0)/FH
FX0, FW = -0.105, 0.21
FZ0, FH = 1.40, 0.21


def face_texture(size=1024, ss=3):
    from PIL import Image, ImageDraw, ImageFilter
    S = size * ss
    skin = rgb255(COL["skin"])
    im = Image.new("RGBA", (S, S), skin + (255,))

    def P(x, z):
        return ((x - FX0) / FW * S, (1 - (z - FZ0) / FH) * S)

    def M(m):
        return m / FW * S

    # blush
    blush = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(blush)
    for sx in (-1, 1):
        cx, cz = sx * 0.060, 1.497
        a, b = P(cx - 0.020, cz + 0.011), P(cx + 0.020, cz - 0.011)
        bd.ellipse([a, b], fill=rgb255(COL["blush"]) + (110,))
    blush = blush.filter(ImageFilter.GaussianBlur(M(0.008)))
    im.alpha_composite(blush)

    d = ImageDraw.Draw(im)
    brow = rgb255(COL["brow"])
    lash = (44, 24, 16, 255)
    for sx in (-1, 1):
        cx, cz = sx * 0.043, 1.534
        inner, outer = cx - sx * 0.022, cx + sx * 0.023

        def lid(t, top):
            x = inner + (outer - inner) * t
            # almond: upper lid peaks a bit toward the outer side
            if top:
                z = cz + 0.001 + 0.0200 * math.sin(math.pi * t) ** 0.8 + 0.004 * t
            else:
                z = cz - 0.002 - 0.0135 * math.sin(math.pi * t) ** 0.9 + 0.003 * t
            return x, z

        up = [lid(i / 24, True) for i in range(25)]
        lo = [lid(i / 24, False) for i in range(25)]
        shape = [P(*q) for q in up] + [P(*q) for q in reversed(lo)]
        # eye white + iris clipped to the eye shape
        eye = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ed = ImageDraw.Draw(eye)
        ed.polygon(shape, fill=(250, 245, 238, 255))
        ix, iz = cx + sx * 0.0005, cz + 0.0035
        r = 0.0148
        ed.ellipse([P(ix - r, iz + r), P(ix + r, iz - r)], fill=rgb255(COL["iris"]) + (255,))
        r2 = 0.0110
        ed.ellipse([P(ix - r2, iz + r2 - 0.002), P(ix + r2, iz - r2 - 0.002)], fill=(150, 92, 50, 255))
        r3 = 0.0060
        ed.ellipse([P(ix - r3, iz + r3), P(ix + r3, iz - r3)], fill=(28, 16, 10, 255))
        h = 0.0032
        hx, hz = ix + 0.004, iz + 0.005
        ed.ellipse([P(hx - h, hz + h), P(hx + h, hz - h)], fill=(255, 255, 255, 255))
        h2 = 0.0016
        hx, hz = ix - 0.005, iz - 0.005
        ed.ellipse([P(hx - h2, hz + h2), P(hx + h2, hz - h2)], fill=(255, 255, 255, 230))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).polygon(shape, fill=255)
        eye.putalpha(Image.composite(eye.getchannel("A"), Image.new("L", (S, S), 0), mask))
        # soft lid shadow on the top of the eyeball
        im.alpha_composite(eye)
        d = ImageDraw.Draw(im)
        # upper lash line (thick, with outer flick) and thin lower line
        pts = [P(*q) for q in up]
        for i in range(len(pts) - 1):
            t = i / (len(pts) - 1)
            w = M(0.0012 + 0.0032 * math.sin(math.pi * min(1, t * 1.15)) ** 0.5)
            d.line([pts[i], pts[i + 1]], fill=lash, width=max(1, int(w)))
            d.ellipse([pts[i][0] - w / 2, pts[i][1] - w / 2, pts[i][0] + w / 2, pts[i][1] + w / 2], fill=lash)
        ox, oz = up[-1]
        d.line([P(ox, oz), P(ox + sx * 0.006, oz + 0.004)], fill=lash, width=int(M(0.0022)))
        lpts = [P(*q) for q in lo[4:22]]
        d.line(lpts, fill=(150, 95, 70, 200), width=int(M(0.0009)))
        # crease above the eye
        cpts = [P(inner + (outer - inner) * t, cz + 0.024 + 0.004 * math.sin(math.pi * t)) for t in
                [i / 12 for i in range(2, 11)]]
        d.line(cpts, fill=rgb255(COL["skin_shadow"]) + (255,), width=int(M(0.0010)))
        # eyebrow: tapered arc
        bpts = []
        for i in range(21):
            t = i / 20
            x = cx - sx * 0.021 + sx * 0.047 * t
            z = 1.566 + 0.010 * math.sin(math.pi * (t * 0.85 + 0.1)) - 0.004 * t
            bpts.append((P(x, z), M(0.0055 * (1 - 0.75 * t) + 0.0012)))
        for (p, w) in bpts:
            d.ellipse([p[0] - w / 2, p[1] - w / 2, p[0] + w / 2, p[1] + w / 2], fill=brow + (255,))

    # nose: a faint shadow under the tip
    a_, b_ = P(-0.007, 1.4735), P(0.007, 1.4695)
    d.ellipse([a_, b_], fill=rgb255(COL["skin_shadow"]) + (70,))
    # mouth: small closed smile
    mp = [P(-0.017 + 0.034 * t, 1.4500 - 0.0045 * math.sin(math.pi * t) + 0.0022 * (abs(t - 0.5) * 2) ** 2)
          for t in [i / 20 for i in range(21)]]
    d.line(mp, fill=(140, 60, 52, 255), width=int(M(0.0018)), joint="curve")
    # lower lip hint
    lp = [P(-0.009 + 0.018 * t, 1.4435 - 0.0025 * math.sin(math.pi * t)) for t in [i / 10 for i in range(11)]]
    d.line(lp, fill=rgb255(COL["lip"]) + (160,), width=int(M(0.0022)), joint="curve")

    im = im.resize((size, size), Image.LANCZOS).convert("RGB")
    return save_image(os.path.join(OUT_TEX, "face.png"), im, "face")


# --------------------------------------------------------------------------
# Body parts
# --------------------------------------------------------------------------
HEAD_C = V(0.0, -0.014, 1.510)
HEAD_R = V(0.101, 0.103, 0.114)
# face texture features are authored for a 0.090 x 0.104 head; scale into the real head
KX, KZ = HEAD_R.x / 0.090, HEAD_R.z / 0.104


def face_uv(x, z):
    fx = x / KX
    fz = (z - HEAD_C.z) / KZ + 1.522
    return (fx - FX0) / FW, (fz - FZ0) / FH


def build_head(mat):
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=96, v_segments=64, radius=1.0)
    for v in bm.verts:
        x, y, z = v.co
        t = max(0.0, -z)          # lower half
        up = max(0.0, z)
        # jaw / chin taper
        x *= 1.0 - 0.26 * t ** 2.2
        ry = 1.0
        if y > 0:                 # back of the head
            ry *= 1.0 - 0.38 * t ** 1.2 + 0.06 * up
        else:                     # face
            ry *= 1.0 - 0.10 * t ** 2
        y *= ry
        # slightly flatter face plane
        if y < 0:
            y *= 0.97 + 0.03 * min(1, abs(x) * 1.5)
        if y < 0 and z < 0:       # longer lower face so the chin clears the neck
            z *= 1.0 + 0.16 * t * min(1.0, -y * 2.0)
        co = Vector((x * HEAD_R.x, y * HEAD_R.y, z * HEAD_R.z))
        # chin forward
        if co.y < 0:
            co.y -= 0.016 * t ** 3
        # cheeks
        for sx in (-1, 1):
            dx, dz = (co.x - sx * 0.060) / 0.034, (co.z + 0.03) / 0.034
            if co.y < 0:
                co.y -= 0.004 * math.exp(-(dx * dx + dz * dz))
        # nose bump
        if co.y < 0:
            nz = co.z - (1.482 - 1.522) * KZ
            g = math.exp(-(co.x / 0.0095) ** 2 - (nz / (0.020 if nz > 0 else 0.008)) ** 2)
            co.y -= 0.017 * g * (0.5 + 0.5 * min(1, max(0, 1 - nz / 0.03)))
            # mouth / muzzle and a little chin
            mz = co.z - (1.450 - 1.522) * KZ
            co.y -= 0.006 * math.exp(-(co.x / 0.025) ** 2 - (mz / 0.012) ** 2)
            cz_ = co.z - (1.425 - 1.522) * KZ
            co.y -= 0.004 * math.exp(-(co.x / 0.018) ** 2 - (cz_ / 0.008) ** 2)
            # eye sockets slightly inset, brow slopes back above
            for sx in (-1, 1):
                ez = co.z - (1.535 - 1.522) * KZ
                co.y += 0.004 * math.exp(-((co.x - sx * 0.047) / 0.022) ** 2 - (ez / 0.014) ** 2)
            if co.z > 0.045:
                co.y += 0.06 * (co.z - 0.045) ** 1.5 * 3
        v.co = co + HEAD_C
    me = bpy.data.meshes.new("Head")
    bm.to_mesh(me)
    bm.free()
    ob = link(bpy.data.objects.new("Head", me))
    shade_smooth(ob)
    ob.data.materials.append(mat)
    # UVs: front planar projection for the face, back of head -> a skin corner
    uv = me.uv_layers.new(name="UVMap")
    for loop in me.loops:
        co = me.vertices[loop.vertex_index].co
        if co.y < HEAD_C.y + 0.02:
            u, vv = face_uv(co.x, co.z)
        else:
            u, vv = 0.01, 0.01
        uv.data[loop.index].uv = (min(max(u, 0.005), 0.995), min(max(vv, 0.005), 0.995))
    return ob


def build_head_from_drawing(mat):
    sys.path.insert(0, HERE)
    import head_from_drawing as HD
    verts, faces = HD.build_mesh()
    ob = mesh_obj("Head", verts, faces, mat)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    ob["from_drawing"] = True
    return ob


def build_ears_from_drawing(mat):
    import head_from_drawing as HD
    objs = []
    for sx in (-1, 1):
        c, (rx, ry, rz), yaw = HD.ear_frame(sx)
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=14, radius=1.0)
        for v in bm.verts:
            x, y, z = v.co
            # a flattened shell: thicker at the rim, slightly cupped toward the front
            v.co = Vector((x * rx * (1.0 - 0.3 * max(0.0, -y)), y * ry, z * rz * (1 - 0.15 * max(0.0, z))))
        me = bpy.data.meshes.new("Ear")
        bm.to_mesh(me)
        bm.free()
        ob = link(bpy.data.objects.new("Ear", me))
        me.transform(Matrix.Translation(V(*c)) @ Matrix.Rotation(yaw, 4, "Z") @ Matrix.Rotation(0.12, 4, "X"))
        shade_smooth(ob)
        me.materials.append(mat)
        objs.append(ob)
    return objs


def build_ears(mat):
    objs = []
    for sx in (-1, 1):
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=1.0)
        for v in bm.verts:
            x, y, z = v.co
            v.co = Vector((x * 0.009, y * 0.018, z * 0.027))
        me = bpy.data.meshes.new("Ear")
        bm.to_mesh(me)
        bm.free()
        ob = link(bpy.data.objects.new("Ear", me))
        mw = Matrix.Translation(V(sx * 0.096, 0.004, HEAD_C.z - 0.017)) @ Matrix.Rotation(-sx * 0.25, 4, "Z") @ Matrix.Rotation(0.15, 4, "X")
        me.transform(mw)
        shade_smooth(ob)
        me.materials.append(mat)
        objs.append(ob)
    return objs


def arm_nodes(sx):
    return {
        "shoulder": V(sx * 0.160, 0.012, 1.300),
        "elbow": V(sx * 0.200, 0.024, 1.090),
        "cuff": V(sx * 0.214, 0.004, 0.965),
        "wrist": V(sx * 0.222, 0.000, 0.860),
        "knuckle": V(sx * 0.226, -0.004, 0.775),
    }


def join(objs, name):
    """Merge baked mesh objects (identity transforms) into one."""
    bm = bmesh.new()
    for o in objs:
        bm.from_mesh(o.data)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    mats = objs[0].data.materials[:]
    for o in objs:
        bpy.data.objects.remove(o)
    ob = link(bpy.data.objects.new(name, me))
    for m in mats:
        me.materials.append(m)
    shade_smooth(ob)
    return ob


def cylinder_uv(ob, split_z, tile=0.105):
    """Plaid UVs: unwrap each leg (and the pelvis) around its own vertical axis
    so the checks stay square and vertical like real fabric."""
    me = ob.data
    uv = me.uv_layers.new(name="UVMap")
    for poly in me.polygons:
        c = poly.center
        if c.z < split_z:
            ax = V(0.10 if c.x > 0 else -0.10, 0.012, 0)
            seam = 0.0   # seam on the inner side of the leg
            sgn = 1 if c.x > 0 else -1
        else:
            ax, seam, sgn = V(0, 0.01, 0), 0.0, 1
        us = []
        for li in poly.loop_indices:
            co = me.vertices[me.loops[li].vertex_index].co
            dx, dy = (co.x - ax.x) * sgn, co.y - ax.y
            if c.z < split_z:
                ang = math.atan2(dy, -dx)            # 0 = inner side
            else:
                ang = math.atan2(dx, dy)             # 0 = back centre
            r = 0.09 if c.z < split_z else 0.15
            us.append((ang, co.z, r))
        base = us[0][0]
        for li, (ang, z, r) in zip(poly.loop_indices, us):
            while ang - base > math.pi:
                ang -= 2 * math.pi
            while ang - base < -math.pi:
                ang += 2 * math.pi
            uv.data[li].uv = (ang * r / tile / 4, z / tile / 4)


# --------------------------------------------------------------------------
# Base body: one continuous skinned mesh (neck to toes) wearing plain underwear.
# Every garment is fitted to and weighted from this body.
# --------------------------------------------------------------------------
def hand_tree(sx, n, e, wrist_i):
    """Append palm, fingers and thumb nodes to (n, e), attached at node `wrist_i`."""
    a = arm_nodes(sx)
    w, k = a["wrist"], a["knuckle"]
    palm = w.lerp(k, 0.5)
    ip = len(n)
    n += [(palm, (0.021, 0.038)), (k, (0.017, 0.038))]
    e += [(wrist_i, ip), (ip, ip + 1)]
    # fingers spread front-to-back along Y, curl toward the thigh (-sx)
    fingers = [(-0.027, 0.080, 0.0085), (-0.009, 0.088, 0.0087), (0.009, 0.083, 0.0083), (0.027, 0.068, 0.0075)]
    for fy, ln, r in fingers:
        base = k + V(0, fy, 0.004)
        i = len(n)
        p1 = base + V(-sx * 0.004, 0, -ln * 0.40)
        p2 = p1 + V(-sx * 0.007, 0, -ln * 0.32)
        p3 = p2 + V(-sx * 0.009, 0, -ln * 0.25)
        n += [(base, (r * 1.1, r * 1.1)), (p1, (r, r)), (p2, (r * 0.92, r * 0.92)), (p3, (r * 0.8, r * 0.8))]
        e += [(ip + 1, i), (i, i + 1), (i + 1, i + 2), (i + 2, i + 3)]
    i = len(n)
    t0 = palm + V(-sx * 0.006, -0.026, 0.012)
    t1 = t0 + V(-sx * 0.006, -0.014, -0.024)
    t2 = t1 + V(-sx * 0.006, -0.006, -0.024)
    n += [(t0, (0.011, 0.011)), (t1, (0.0088, 0.0088)), (t2, (0.0075, 0.0075))]
    e += [(ip, i), (i, i + 1), (i + 1, i + 2)]


def foot_frame(sx):
    ax = sx * 0.124
    yaw = Matrix.Rotation(-sx * 0.12, 4, "Z")   # toes slightly outward

    def F(x, y, z):
        p = yaw @ Vector((x * sx * 1.45, y * 1.28, z * 1.15))
        return V(ax + p.x, 0.030 + p.y, z)
    return F


def foot_tree(sx, n, e, ankle_i):
    F = foot_frame(sx)
    i = len(n)
    n += [
        (F(0, 0.030, 0.034), (0.034, 0.030)),       # heel
        (F(0.002, -0.040, 0.034), (0.040, 0.026)),  # midfoot
        (F(0.004, -0.105, 0.022), (0.046, 0.020)),  # ball
    ]
    e += [(ankle_i, i), (ankle_i, i + 1), (i + 1, i + 2)]
    ball = i + 2
    toes = [(-0.026, 0.036, 0.0115), (-0.009, 0.030, 0.0088), (0.005, 0.027, 0.0082),
            (0.018, 0.024, 0.0076), (0.030, 0.019, 0.0070)]
    for tx, ln, r in toes:
        j = len(n)
        n += [(F(tx, -0.118, 0.020), (r * 1.15, r)), (F(tx * 1.08, -0.118 - ln, 0.014), (r, r * 0.9))]
        e += [(ball, j), (j, j + 1)]


def build_body(m_skin, m_under):
    n = [
        (V(0, 0.014, 0.860), (0.134, 0.100)),   # 0 pelvis (root)
        (V(0, 0.006, 0.990), (0.110, 0.082)),   # 1 waist
        (V(0, -0.004, 1.130), (0.126, 0.098)),  # 2 chest
        (V(0, 0.004, 1.245), (0.116, 0.080)),   # 3 upper chest
        (V(0, 0.012, 1.340), (0.064, 0.058)),   # 4 neck base
        (V(0, 0.014, 1.460), (0.054, 0.052)),   # 5 neck top (inside the head)
    ]
    e = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]
    for sx in (1, -1):
        a = arm_nodes(sx)
        i = len(n)
        n += [
            (a["shoulder"], (0.044, 0.046)),
            (a["shoulder"].lerp(a["elbow"], 0.5), (0.038, 0.040)),
            (a["elbow"], (0.032, 0.034)),
            (a["elbow"].lerp(a["wrist"], 0.4), (0.033, 0.034)),
            (a["wrist"], (0.024, 0.031)),
        ]
        e += [(3, i), (i, i + 1), (i + 1, i + 2), (i + 2, i + 3), (i + 3, i + 4)]
        hand_tree(sx, n, e, i + 4)
        i = len(n)
        F = foot_frame(sx)
        n += [
            (V(sx * 0.082, 0.014, 0.790), (0.078, 0.086)),   # hip / upper thigh
            (V(sx * 0.093, 0.008, 0.640), (0.064, 0.070)),
            (V(sx * 0.100, 0.004, 0.505), (0.050, 0.054)),   # knee
            (V(sx * 0.110, 0.018, 0.360), (0.050, 0.054)),   # calf
            (V(sx * 0.118, 0.026, 0.190), (0.036, 0.038)),
            (F(0, 0, 0.075), (0.033, 0.036)),                # ankle
        ]
        e += [(0, i), (i, i + 1), (i + 1, i + 2), (i + 2, i + 3), (i + 3, i + 4), (i + 4, i + 5)]
        foot_tree(sx, n, e, i + 5)
    ob = skin_mesh("Body", n, e, subdiv=2, mat=m_skin)
    for v in ob.data.vertices:   # flatten the soles
        if v.co.z < 0.004:
            v.co.z = 0.004 + (v.co.z - 0.004) * 0.15
    return ob


def weights_of(ob):
    names = {g.index: g.name for g in ob.vertex_groups}
    return [{names[g.group]: g.weight for g in v.groups if g.weight > 1e-4} for v in ob.data.vertices]


def dominant(ws):
    return max(ws.items(), key=lambda kv: kv[1])[0] if ws else "hips"


def rig_body(body, rig):
    """Bone-heat (automatic) weights, falling back to distance weights for any vertex heat missed."""
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    body.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    try:
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
    except Exception as ex:   # pragma: no cover - depends on Blender build
        print("bone heat failed:", ex)
    if not any(m.type == "ARMATURE" for m in body.modifiers):
        m = body.modifiers.new("Armature", "ARMATURE")
        m.object = rig
        body.parent = rig
    ws = weights_of(body)
    segs = [(name, h, t) for name, h, t, _ in BONES]
    missing = 0
    for v, w in zip(body.data.vertices, ws):
        if sum(w.values()) > 0.05:
            continue
        missing += 1
        best = sorted(((1 / (seg_dist(v.co, h, t) + 0.01) ** 6, nm) for nm, h, t in segs), reverse=True)[:2]
        tot = sum(x for x, _ in best)
        for x, nm in best:
            g = body.vertex_groups.get(nm) or body.vertex_groups.new(name=nm)
            g.add([v.index], x / tot, "REPLACE")
    print(f"body weights: {missing} vertices needed the distance fallback")


# Body sections a garment can hide while worn (saves fill-rate and stops poke-through)
SECTION_OF_BONE = {
    "hips": "pelvis", "spine": "torso", "chest": "torso", "neck": "neck", "head": "neck",
    "shoulder": "torso", "upper_arm": "upperarms", "forearm": "forearms", "hand": "hands",
    "thigh": "thighs", "shin": "shins", "foot": "feet", "toe": "feet",
}


def section_of(co, bone):
    sec = SECTION_OF_BONE[bone.split(".")[0]]
    if sec == "torso" and co.z > 1.20:
        return "neck"          # upper chest shows through necklines: never hidden
    if sec == "shins" and co.z < 0.17:
        return "feet"          # ankles show below cuffs: never hidden
    return sec


def classify_body(body):
    ws = weights_of(body)
    me = body.data
    face_sec, face_bone = [], []
    for p in me.polygons:
        tally = {}
        for vi in p.vertices:
            b = dominant(ws[vi])
            tally[b] = tally.get(b, 0) + 1
        b = max(tally.items(), key=lambda kv: kv[1])[0]
        face_bone.append(b)
        face_sec.append(section_of(p.center, b))
        # plain underwear: a sports-top band and briefs
        c = p.center
        base = b.split(".")[0]
    return face_sec, face_bone, ws


def build_underwear(body, face_sec, ws, rig, mat):
    """Plain sports top and briefs: copies of the body surface between clean cut lines,
    lifted slightly off the skin. They carry the body's own weights."""
    me = body.data
    bands = [("top", {"torso", "neck"}, 1.090, 1.232), ("briefs", {"pelvis", "thighs", "torso"}, 0.772, 0.912)]
    pieces = []
    for name, secs, z0, z1 in bands:
        bm = bmesh.new()
        bm.from_mesh(me)
        orig = bm.verts.layers.int.new("orig")
        for v in bm.verts:
            v[orig] = v.index
        bm.faces.ensure_lookup_table()
        kill = [f for f in bm.faces
                if not (face_sec[f.index] in secs and z0 < f.calc_center_median().z < z1)
                or (name == "top" and abs(f.calc_center_median().x) > 0.135)]
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
        for v in bm.verts:
            if v.is_boundary and name == "briefs" or (v.is_boundary and abs(v.co.x) < 0.12):
                v.co.z = z0 if v.co.z < (z0 + z1) / 2 else z1
            v.co += v.normal * 0.0025
        idx = [v[orig] for v in bm.verts]
        bm.verts.layers.int.remove(orig)
        ume = bpy.data.meshes.new("Underwear_" + name)
        bm.to_mesh(ume)
        bm.free()
        ob = link(bpy.data.objects.new("Underwear_" + name, ume))
        ume.materials.append(mat)
        shade_smooth(ob)
        groups = {}
        for i, oi in enumerate(idx):
            for b, w in ws[oi].items():
                g = groups.get(b) or groups.setdefault(b, ob.vertex_groups.new(name=b))
                g.add([i], w, "REPLACE")
        pieces.append(ob)
    out = {}
    for ob, (name, *_r) in zip(pieces, bands):
        ob.parent = rig
        mod = ob.modifiers.new("Armature", "ARMATURE")
        mod.object = rig
        ob["wardrobe_slot"] = "body"
        ob["body_section"] = "underwear_" + name
        out["underwear_" + name] = ob
    return out


def split_body(body, face_sec):
    """Split the body into section objects that keep weights and seamless normals."""
    me = body.data
    normals = [v.normal.copy() for v in me.vertices]
    sections = {}
    for name in sorted(set(face_sec)):
        bm = bmesh.new()
        bm.from_mesh(me)
        orig = bm.verts.layers.int.new("orig")
        for v in bm.verts:
            v[orig] = v.index
        bm.faces.ensure_lookup_table()
        kill = [f for f in bm.faces if face_sec[f.index] != name]
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        loose = [v for v in bm.verts if not v.link_faces]
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
        idx = [v[orig] for v in bm.verts]
        bm.verts.layers.int.remove(orig)
        sme = bpy.data.meshes.new("Body_" + name)
        bm.to_mesh(sme)
        bm.free()
        for m in me.materials:
            sme.materials.append(m)
        for poly in sme.polygons:
            poly.use_smooth = True
        sme.normals_split_custom_set_from_vertices([normals[i] for i in idx])
        so = link(bpy.data.objects.new("Body_" + name, sme))
        for g in body.vertex_groups:
            so.vertex_groups.new(name=g.name)
        so.parent = body.parent
        mod = so.modifiers.new("Armature", "ARMATURE")
        mod.object = body.parent
        so["body_section"] = name
        so["wardrobe_slot"] = "body"
        sections[name] = so
    return sections


class BodySampler:
    """Nearest-point queries against (a subset of) the base body surface."""

    def __init__(self, body, face_sec, ws, allowed=None):
        me = body.data
        self.me, self.ws = me, ws
        self.faces = [p.index for p in me.polygons if allowed is None or face_sec[p.index] in allowed]
        verts = [v.co.copy() for v in me.vertices]
        self.bvh = BVHTree.FromPolygons(verts, [tuple(me.polygons[i].vertices) for i in self.faces])

    def nearest(self, co):
        loc, nrm, fi, dist = self.bvh.find_nearest(co)
        return loc, nrm, self.me.polygons[self.faces[fi]]

    def weights_at(self, co):
        loc, nrm, poly = self.nearest(co)
        acc = {}
        for vi in poly.vertices:
            k = 1.0 / ((self.me.vertices[vi].co - loc).length + 1e-5)
            for b, w in self.ws[vi].items():
                acc[b] = acc.get(b, 0.0) + w * k
        top = sorted(acc.items(), key=lambda kv: -kv[1])[:4]
        tot = sum(w for _, w in top) or 1.0
        return {b: w / tot for b, w in top}


def fit_outside(ob, sampler, margin=0.006, depth=0.025):
    """Push fabric vertices that sit inside (or on) the skin back out by `margin`."""
    moved = 0
    for v in ob.data.vertices:
        loc, nrm, _ = sampler.nearest(v.co)
        s = (v.co - loc).dot(nrm)
        if -depth < s < margin:
            v.co += nrm * (margin - s)
            moved += 1
    return moved


def bind_garment(ob, rig, sampler):
    """Copy skin weights from the body under the garment so it deforms with the body."""
    ob.parent = rig
    mod = ob.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    groups = {}
    for v in ob.data.vertices:
        for b, w in sampler.weights_at(v.co).items():
            g = groups.get(b) or groups.setdefault(b, ob.vertex_groups.new(name=b))
            g.add([v.index], w, "REPLACE")


def cut_faces(ob, kill_fn, snap_fn=None):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    kill = [f for f in bm.faces if kill_fn(f.calc_center_median())]
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    if snap_fn:
        for v in bm.verts:
            if v.is_boundary:
                snap_fn(v)
    bm.to_mesh(ob.data)
    bm.free()


def solidify(ob, thickness):
    """Garments are single-layer cloth with double-sided materials: an inner shell
    would poke through wherever fitting pulls the fabric in, and costs triangles."""
    if thickness <= 0 or os.environ.get("CLOTH_SINGLE", "1") == "1":
        shade_smooth(ob)
        return
    so = ob.modifiers.new("Solidify", "SOLIDIFY")
    so.thickness = thickness
    so.offset = 1.0
    so.use_rim = True
    bake(ob)
    shade_smooth(ob)


# --------------------------------------------------------------------------
# Garments. Each is built from the same skeleton layout as the body with some
# ease added, cut to length, pushed outside the skin, then bound to the rig
# with weights copied from the body.
# --------------------------------------------------------------------------
def neckline_z(co, depth=0.050, top=1.355):
    r = math.hypot(co.x, (co.y - 0.01) * 1.25)
    front = max(0.0, -(co.y - 0.01) / max(r, 1e-4))
    return top - depth * front ** 1.5, r


def build_top(name, mat, rig, body, face_sec, ws, *, ease=0.95, hem=0.928, sleeve_cut=None,
              sleeve_r=(0.052, 0.046, 0.052, 0.042), neck_depth=0.030, neck_r=0.100):
    """Sweater-like top. sleeve_cut: z of the sleeve end (None = to the cuff)."""
    # one skin-modifier graph: torso with both sleeves branching off the upper chest,
    # so the shoulder joins are seamless; neckline, hem and cuffs are cut afterwards
    r0, r1, r2, r3 = sleeve_r
    n = [
        (V(0, 0.002, hem - 0.07), (0.188 * ease, 0.136 * ease)),   # 0
        (V(0, -0.004, 1.030), (0.178 * ease, 0.128 * ease)),      # 1
        (V(0, -0.010, 1.160), (0.165 * ease, 0.124 * ease)),      # 2
        (V(0, 0.004, 1.270), (0.135, 0.096)),                     # 3 upper chest
        (V(0, 0.010, 1.370), (0.066, 0.060)),                     # 4 neck (cut away)
    ]
    e = [(0, 1), (1, 2), (2, 3), (3, 4)]
    ends = {}
    for sx in (1, -1):
        a = arm_nodes(sx)
        i = len(n)
        zc = (a["cuff"].z - 0.008) if sleeve_cut is None else sleeve_cut
        # arm axis point at the sleeve end height
        axis = [a["shoulder"], a["elbow"], a["cuff"]]
        if zc >= a["elbow"].z:
            t = (a["shoulder"].z - zc) / (a["shoulder"].z - a["elbow"].z)
            end = a["shoulder"].lerp(a["elbow"], t)
            dirv = (a["elbow"] - a["shoulder"]).normalized()
            chain = [(a["shoulder"], (r0, r0)), (end - dirv * 0.02, (r1 * 1.02, r1 * 1.02)),
                     (end + dirv * 0.03, (r1, r1))]
        else:
            t = (a["elbow"].z - zc) / (a["elbow"].z - a["cuff"].z)
            end = a["elbow"].lerp(a["cuff"], t)
            dirv = (a["cuff"] - a["elbow"]).normalized()
            chain = [(a["shoulder"], (r0, r0)), (a["elbow"], (r1, r1)),
                     (a["elbow"].lerp(end, 0.6) + V(sx * 0.003, 0, 0), (r2, r2)),
                     (end, (r3, r3)), (end + dirv * 0.04, (r3 * 0.92, r3 * 0.92))]
        n += chain
        e += [(3, i)] + [(i + k, i + k + 1) for k in range(len(chain) - 1)]
        ends[sx] = (end, dirv, max(r1, r2, r3) * 1.35)
    whole = skin_mesh(name + "Whole", n, e, subdiv=2)

    def beyond(co):
        for sx, (end, dirv, rad) in ends.items():
            d = co - end
            along = d.dot(dirv)
            radial = (d - dirv * along).length
            if along > -0.002 and radial < rad and (co.x * sx) > 0.12:
                return sx, along
        return None

    def kill(c):
        z, r = neckline_z(c, neck_depth)
        if c.z > z and r < neck_r:
            return True
        if beyond(c):
            return True
        return c.z < hem and abs(c.x) < 0.19

    def snap(v):
        best = None
        for sx, (end, dirv, rad) in ends.items():
            d = v.co - end
            along = d.dot(dirv)
            radial = (d - dirv * along).length
            if abs(along) < 0.03 and radial < rad and (v.co.x * sx) > 0.12:
                best = (end, dirv, along)
        if best:
            end, dirv, along = best
            v.co = v.co - dirv * along
        elif v.co.z < 1.0:
            v.co.z = hem
        elif neckline_z(v.co, neck_depth)[1] < neck_r + 0.02:
            v.co.z = neckline_z(v.co, neck_depth)[0]
    cut_faces(whole, kill, snap)
    # split into torso / sleeves only for weight sampling, by distance to the arm chain
    bm = bmesh.new()
    bm.from_mesh(whole.data)
    parts = []
    for part in ("torso", 1, -1):
        pb = bm.copy()
        pb.faces.ensure_lookup_table()
        def which(c):
            if abs(c.x) < 0.15 or c.z > 1.24 and abs(c.x) < 0.17:
                return "torso"
            return 1 if c.x > 0 else -1
        killf = [f for f in pb.faces if which(f.calc_center_median()) != part]
        bmesh.ops.delete(pb, geom=killf, context="FACES")
        me = bpy.data.meshes.new(f"{name}_{part}")
        pb.to_mesh(me)
        pb.free()
        parts.append(link(bpy.data.objects.new(f"{name}_{part}", me)))
    bm.free()
    bpy.data.objects.remove(whole)
    s_torso = BodySampler(body, face_sec, ws, {"torso", "pelvis", "neck"})
    s_arms = BodySampler(body, face_sec, ws, {"upperarms", "forearms"})
    fit_outside(parts[0], s_torso)
    for o in parts[1:]:
        fit_outside(o, s_arms)
    for o in parts:
        solidify(o, 0.006)
    # bind each piece against the right part of the body, then merge
    for o, smp in zip(parts, (s_torso, s_arms, s_arms)):
        bind_garment(o, rig, smp)
    ob = join_skinned(parts, name, rig)
    bm = bmesh.new()                       # weld the split seams back together
    bm.from_mesh(ob.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.to_mesh(ob.data)
    bm.free()
    shade_smooth(ob)
    ob.data.materials.append(mat)
    return ob


def build_bottom(name, mat, m_string, rig, body, face_sec, ws, *, cut=0.092, leg_ease=1.0):
    n = [
        (V(0, 0.004, 0.975), (0.138, 0.104)),
        (V(0, 0.012, 0.870), (0.146, 0.114)),
    ]
    e = [(0, 1)]
    for sx in (1, -1):
        i = len(n)
        k = leg_ease
        n += [
            (V(sx * 0.082, 0.010, 0.770), (0.086 * k, 0.104 * k)),
            (V(sx * 0.100, 0.004, 0.510), (0.070 * k, 0.078 * k)),
            (V(sx * 0.114, 0.012, 0.260), (0.066, 0.072)),
            (V(sx * 0.121, 0.018, 0.165), (0.076, 0.078)),
            (V(sx * 0.124, 0.022, 0.115), (0.070, 0.074)),
            (V(sx * 0.124, 0.022, 0.085), (0.060, 0.064)),
        ]
        e += [(1, i), (i, i + 1), (i + 1, i + 2), (i + 2, i + 3), (i + 3, i + 4), (i + 4, i + 5)]
    ob = skin_mesh(name, n, e, subdiv=2)
    if cut > 0.3:   # shorts: add a little flare at the leg opening
        for v in ob.data.vertices:
            if v.co.z < 0.80:
                t = min(1.0, (0.80 - v.co.z) / (0.80 - cut))
                c = V(math.copysign(0.086, v.co.x), 0.010, v.co.z)
                off = v.co - c
                off.z = 0
                v.co = c + off * (1.0 + 0.10 * t) + V(0, 0, v.co.z - c.z)
    cut_faces(ob, lambda c: c.z < cut, lambda v: setattr(v.co, "z", cut))
    smp = BodySampler(body, face_sec, ws, {"pelvis", "thighs", "shins", "torso", "feet"})
    fit_outside(ob, smp)
    solidify(ob, 0.005)
    cylinder_uv(ob, 0.80)
    ob.data.materials.append(mat)
    bind_garment(ob, rig, smp)
    string = build_drawstring(m_string)
    bind_garment(string, rig, BodySampler(body, face_sec, ws, {"pelvis", "torso"}))
    return join_skinned([ob, string], name, rig)


def join_skinned(objs, name, rig):
    """Join garment pieces (UVs, vertex groups and materials are kept by Blender's join)."""
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        for m in list(o.modifiers):
            if m.type == "ARMATURE":
                o.modifiers.remove(m)
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    ob = objs[0]
    ob.name = ob.data.name = name
    ob.parent = rig
    mod = ob.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    shade_smooth(ob)
    return ob

def build_drawstring(mat):
    g = GeoBatch()
    front = lambda x, z: V(x, -0.118 - 0.012 * (1 - (x / 0.05) ** 2), z)
    knot = V(0.0, -0.110, 0.918)
    # loops of the bow
    for sx in (-1, 1):
        ctrl = [knot, knot + V(sx * 0.020, -0.004, 0.012), knot + V(sx * 0.040, -0.006, 0.006),
                knot + V(sx * 0.034, -0.004, -0.010), knot + V(sx * 0.006, -0.002, -0.002)]
        g.add(*sweep_geo(catmull(ctrl, 6), 0.0045, 0.0030, ring=6))
        # hanging tails
        ctrl = [knot, knot + V(sx * 0.008, -0.003, -0.020), knot + V(sx * 0.010, -0.002, -0.045),
                knot + V(sx * 0.006, -0.000, -0.070)]
        g.add(*sweep_geo(catmull(ctrl, 6), lambda t: 0.0036, 0.0028, ring=6))
        # aglet / end knot
        g.add(*sweep_geo([knot + V(sx * 0.006, 0.0, -0.068), knot + V(sx * 0.006, 0.0, -0.078)], 0.0048, 0.0048, ring=8))
    # knot
    g.add(*sweep_geo(catmull([knot + V(-0.006, 0, 0), knot + V(0, -0.004, 0), knot + V(0.006, 0, 0)], 4),
                     0.0065, 0.0055, ring=8))
    # eyelets
    return g.build("Drawstring", mat)


# --------------------------------------------------------------------------
# Hair
# --------------------------------------------------------------------------
BUN_C = V(0.0, 0.078, 1.668)
BUN_R = 0.100


def hairline_z(phi):
    """z of the hairline as a function of azimuth (0 = front, pi = back)."""
    a = abs(phi)
    table = [(0.0, 1.598), (0.45, 1.592), (0.9, 1.566), (1.25, 1.520), (1.55, 1.505), (1.85, 1.470),
             (2.4, 1.440), (math.pi, 1.432)]
    for (a0, z0), (a1, z1) in zip(table, table[1:]):
        if a <= a1:
            t = (a - a0) / (a1 - a0)
            t = t * t * (3 - 2 * t)
            return HEAD_C.z + (z0 + (z1 - z0) * t - 1.522) * KZ
    return HEAD_C.z + (table[-1][1] - 1.522) * KZ


def H(x, y, z):
    """Map a point authored for the 0.090 x 0.104 head onto the real head."""
    return V(x * KX, HEAD_C.y + (y + 0.012) * KX, HEAD_C.z + (z - 1.522) * KZ)


def head_surface(bvh, direction):
    hit = bvh.ray_cast(HEAD_C + direction * 0.3, -direction)
    if hit[0] is None:
        return HEAD_C + direction * 0.1, direction
    return hit[0], hit[1]


def build_hair(head, mat, mat_dark):
    me = head.data
    bvh = BVHTree.FromPolygons([v.co.copy() for v in me.vertices], [tuple(p.vertices) for p in me.polygons])

    # 1) scalp cap: head surface above the hairline, pushed out a bit
    bm = bmesh.new()
    bm.from_mesh(me)
    kill = []
    for f in bm.faces:
        c = f.calc_center_median()
        phi = math.atan2(c.x, -(c.y - HEAD_C.y))
        if c.z < hairline_z(phi):
            kill.append(f)
    bmesh.ops.delete(bm, geom=kill, context="FACES")
    for v in bm.verts:
        d = (v.co - HEAD_C).normalized()
        v.co += d * 0.009
    cap_me = bpy.data.meshes.new("HairCap")
    bm.to_mesh(cap_me)
    bm.free()
    cap = link(bpy.data.objects.new("HairCap", cap_me))
    cap_me.uv_layers.clear() if hasattr(cap_me.uv_layers, "clear") else None
    cap_me.materials.clear()
    cap_me.materials.append(mat_dark)
    so = cap.modifiers.new("Solidify", "SOLIDIFY")
    so.thickness = 0.006
    so.offset = -1
    bake(cap)
    shade_smooth(cap)

    g = GeoBatch()
    gd = GeoBatch()

    # 2) combed-up strands from the hairline to the bun
    bun_base = BUN_C + V(0, -0.02, -0.055)
    count = 96
    for i in range(count):
        phi = -math.pi + 2 * math.pi * (i + RNG.random() * 0.6) / count
        z0 = hairline_z(phi) + 0.004
        # start direction on the head at that azimuth/height
        d0 = Vector((math.sin(phi), -math.cos(phi), 0))
        dz = (z0 - HEAD_C.z) / HEAD_R.z
        d0 = Vector((d0.x * math.sqrt(max(0, 1 - dz * dz)), d0.y * math.sqrt(max(0, 1 - dz * dz)), dz)).normalized()
        target = (bun_base + V(RNG.uniform(-0.03, 0.03), RNG.uniform(-0.02, 0.02), RNG.uniform(-0.01, 0.02)) - HEAD_C).normalized()
        pts, nrm = [], []
        steps = 14
        for k in range(steps + 1):
            t = k / steps
            dvec = d0.slerp(target, t) if d0.dot(target) > -0.99 else d0
            p, nn = head_surface(bvh, dvec)
            lift = 0.004 + 0.012 * math.sin(math.pi * t) ** 0.7 + 0.003 * RNG.random()
            pts.append(p + nn * lift)
            nrm.append(nn)
        pts = catmull(pts[::2], 4)
        width = lambda t, w=RNG.uniform(0.010, 0.014): w * (0.35 + 0.65 * math.sin(math.pi * min(1, t * 1.1 + 0.05)))
        (gd if i % 5 == 0 else g).add(*sweep_geo(pts, width, 0.0040,
                                                 up=lambda i_, p_: (p_ - HEAD_C).normalized(), ring=6))

    # 3) the messy bun: core + big twisted loops + flyaways
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=24, v_segments=16, radius=BUN_R * 0.86)
    bmesh.ops.translate(bm, verts=bm.verts, vec=BUN_C)
    core_me = bpy.data.meshes.new("BunCore")
    bm.to_mesh(core_me)
    bm.free()
    core = link(bpy.data.objects.new("BunCore", core_me))
    tx = bpy.data.textures.new("BunNoise", "CLOUDS")
    tx.noise_scale = 0.35
    sd = core.modifiers.new("Sub", "SUBSURF")
    sd.levels = 2
    dm = core.modifiers.new("Disp", "DISPLACE")
    dm.texture = tx
    dm.strength = 0.045
    dm.mid_level = 0.5
    bake(core)
    shade_smooth(core)
    core.data.materials.append(mat_dark)

    bun_up = (BUN_C - HEAD_C).normalized()
    for i in range(12):
        # random loop plane, biased so loops wrap around the bun
        axis = Vector((RNG.uniform(-1, 1), RNG.uniform(-1, 1), RNG.uniform(-1, 1))).normalized()
        axis = (axis + bun_up * RNG.uniform(0.0, 1.2)).normalized()
        u = axis.orthogonal().normalized()
        v = axis.cross(u).normalized()
        rad = BUN_R * RNG.uniform(0.55, 0.95)
        off = Vector((RNG.uniform(-1, 1), RNG.uniform(-1, 1), RNG.uniform(-0.6, 1))) * 0.02
        span = RNG.uniform(1.2, 1.9) * math.pi
        a0 = RNG.uniform(0, 2 * math.pi)
        ctrl = []
        for k in range(9):
            a = a0 + span * k / 8
            wob = 1 + 0.12 * math.sin(a * 3 + i)
            ctrl.append(BUN_C + off + (u * math.cos(a) + v * math.sin(a)) * rad * wob)
        pts = catmull(ctrl, 5)
        w0 = RNG.uniform(0.018, 0.026)
        width = lambda t, w=w0: w * (0.35 + 0.65 * math.sin(math.pi * t) ** 0.6)
        (g if i % 3 else gd).add(*sweep_geo(pts, width, lambda t, w=w0: w * 0.40 * (0.4 + 0.6 * math.sin(math.pi * t) ** 0.6),
                                            up=lambda i_, p_: (p_ - BUN_C).normalized(), ring=8))
    # many small coiled curls over the bun surface -> curly texture
    for i in range(40):
        d = Vector((RNG.gauss(0, 1), RNG.gauss(0, 1), RNG.gauss(0, 1))).normalized()
        if d.dot(bun_up) < -0.55:
            continue
        tdir = d.orthogonal().normalized()
        tdir = (Matrix.Rotation(RNG.uniform(0, 6.28), 3, d) @ tdir).normalized()
        length = RNG.uniform(0.06, 0.11)
        hel = RNG.uniform(0.010, 0.016)
        turns = RNG.uniform(1.2, 2.2)
        pts = []
        for k in range(29):
            t = k / 28
            base_dir = (d + tdir * (t - 0.5) * length / BUN_R).normalized()
            c0 = BUN_C + base_dir * BUN_R * RNG.uniform(0.97, 1.0)
            side = base_dir.cross(tdir).normalized()
            a = 2 * math.pi * turns * t
            pts.append(c0 + (side * math.cos(a) + base_dir * math.sin(a)) * hel)
        w0 = RNG.uniform(0.012, 0.016)
        (gd if i % 4 == 0 else g).add(*sweep_geo(pts, lambda t, w=w0: w * (0.4 + 0.6 * math.sin(math.pi * t) ** 0.5),
                                                 lambda t, w=w0: w * 0.45 * (0.4 + 0.6 * math.sin(math.pi * t) ** 0.5), ring=6))
    # flyaway curls from the bun
    for i in range(12):
        d = Vector((RNG.uniform(-1, 1), RNG.uniform(-1, 1), RNG.uniform(-0.3, 1))).normalized()
        start = BUN_C + d * BUN_R * 0.8
        side = d.orthogonal().normalized()
        ctrl = [start]
        for k in range(1, 5):
            ctrl.append(start + d * 0.018 * k + side * 0.012 * math.sin(k * 1.7) + V(0, 0, -0.004 * k * k))
        g.add(*sweep_geo(catmull(ctrl, 5), lambda t: 0.0045 * (1 - t) + 0.0008, lambda t: 0.0025 * (1 - t) + 0.0006, ring=5))

    # 4) loose tendrils framing the face and at the nape
    def tendril(start, drop, out, fwd, waves, w, target):
        ctrl = []
        n = 7
        for k in range(n):
            t = k / (n - 1)
            side = out * t + 0.006 * math.sin(t * math.pi * waves)
            p = start + V(side, fwd * t + 0.004 * math.cos(t * math.pi * waves), -drop * t)
            ctrl.append(p)
        pts = catmull(ctrl, 5)
        target.add(*sweep_geo(pts, lambda t: w * (1 - 0.85 * t), lambda t: w * 0.45 * (1 - 0.8 * t), ring=6))

    for sx in (-1, 1):
        # long face-framing strands in front of the ears
        tendril(H(sx * 0.074, -0.058, 1.585), 0.150, sx * 0.020, -0.010, 3.0, 0.0085, g)
        tendril(H(sx * 0.082, -0.040, 1.570), 0.130, sx * 0.024, 0.004, 2.5, 0.0075, gd)
        tendril(H(sx * 0.066, -0.070, 1.596), 0.090, sx * 0.018, -0.006, 2.0, 0.0065, g)
        tendril(H(sx * 0.088, -0.018, 1.555), 0.110, sx * 0.016, 0.012, 3.5, 0.0070, g)
        # curtain bang sweeping off the forehead
        ctrl = [H(sx * 0.010, -0.098, 1.610), H(sx * 0.032, -0.100, 1.596), H(sx * 0.056, -0.088, 1.582),
                H(sx * 0.074, -0.068, 1.560), H(sx * 0.086, -0.050, 1.530)]
        g.add(*sweep_geo(catmull(ctrl, 5), lambda t: 0.011 * (1 - 0.7 * t), lambda t: 0.004 * (1 - 0.6 * t),
                         up=lambda i_, p_: (p_ - HEAD_C).normalized(), ring=6))
        # hair volume over the ears
        for k in range(3):
            ctrl = [H(sx * 0.080, 0.00 + 0.02 * k, 1.585 - 0.01 * k), H(sx * 0.097, 0.01 + 0.02 * k, 1.545),
                    H(sx * 0.097, 0.03 + 0.02 * k, 1.505), H(sx * 0.086, 0.06 + 0.01 * k, 1.480)]
            (g if k % 2 else gd).add(*sweep_geo(catmull(ctrl, 5), lambda t: 0.014 * (1 - 0.5 * t), 0.006,
                                                up=lambda i_, p_: (p_ - HEAD_C).normalized(), ring=6))
    # nape wisps
    for i, x in enumerate((-0.05, -0.028, -0.006, 0.018, 0.04, 0.06)):
        tendril(H(x, 0.072, 1.462), 0.050 + 0.012 * (i % 3), 0.006 * math.sin(i * 2.1), 0.012, 2.0, 0.006,
                g if i % 2 else gd)

    hair = g.build("HairStrands", mat)
    hair2 = gd.build("HairStrandsDark", mat_dark)
    return [cap, core, hair, hair2]


# --------------------------------------------------------------------------
# Rig + skinning
# --------------------------------------------------------------------------
BONES = [
    # name, head, tail, parent
    ("hips", V(0, 0.008, 0.880), V(0, 0.008, 0.990), None),
    ("spine", V(0, 0.008, 0.990), V(0, 0.004, 1.120), "hips"),
    ("chest", V(0, 0.004, 1.120), V(0, 0.006, 1.300), "spine"),
    ("neck", V(0, 0.008, 1.340), V(0, 0.000, 1.450), "chest"),
    ("head", V(0, 0.000, 1.450), V(0, -0.010, 1.640), "neck"),
]
for sx, s in ((1, "L"), (-1, "R")):
    a = arm_nodes(sx)
    BONES += [
        (f"shoulder.{s}", V(sx * 0.030, 0.010, 1.300), a["shoulder"], "chest"),
        (f"upper_arm.{s}", a["shoulder"], a["elbow"], f"shoulder.{s}"),
        (f"forearm.{s}", a["elbow"], a["wrist"], f"upper_arm.{s}"),
        (f"hand.{s}", a["wrist"], a["knuckle"] + V(0, 0, -0.06), f"forearm.{s}"),
        (f"thigh.{s}", V(sx * 0.090, 0.010, 0.840), V(sx * 0.102, 0.004, 0.500), "hips"),
        (f"shin.{s}", V(sx * 0.102, 0.004, 0.500), V(sx * 0.124, 0.030, 0.075), f"thigh.{s}"),
        (f"foot.{s}", V(sx * 0.124, 0.030, 0.075), V(sx * 0.136, -0.080, 0.022), f"shin.{s}"),
        (f"toe.{s}", V(sx * 0.136, -0.080, 0.022), V(sx * 0.142, -0.150, 0.018), f"foot.{s}"),
    ]


def build_armature():
    arm = bpy.data.armatures.new("Rig")
    ob = link(bpy.data.objects.new("Rig", arm))
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = {}
    for name, h, t, parent in BONES:
        b = arm.edit_bones.new(name)
        b.head, b.tail = h, t
        b.roll = 0
        if parent:
            b.parent = eb[parent]
            b.use_connect = (eb[parent].tail - h).length < 1e-4
        eb[name] = b
    bpy.ops.object.mode_set(mode="OBJECT")
    arm.display_type = "STICK"
    return ob


def seg_dist(p, a, b):
    ab = b - a
    t = max(0.0, min(1.0, (p - a).dot(ab) / max(ab.length_squared, 1e-9)))
    return (p - (a + ab * t)).length


def skin_to(ob, rig, bones, power=5.0, rigid=None):
    """Distance-to-bone weights limited to `bones` (or rigid to one bone)."""
    ob.parent = rig
    mod = ob.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    groups = {b: ob.vertex_groups.new(name=b) for b in ([rigid] if rigid else bones)}
    if rigid:
        groups[rigid].add(range(len(ob.data.vertices)), 1.0, "REPLACE")
        return
    segs = {name: (h, t) for name, h, t, _ in BONES if name in bones}
    for v in ob.data.vertices:
        ws = []
        for b, (h, t) in segs.items():
            d = seg_dist(v.co, h, t) + 0.01
            ws.append((1.0 / d ** power, b))
        ws.sort(reverse=True)
        ws = ws[:3]
        tot = sum(w for w, _ in ws)
        for w, b in ws:
            if w / tot > 0.02:
                groups[b].add([v.index], w / tot, "REPLACE")


# --------------------------------------------------------------------------
# Animations
# --------------------------------------------------------------------------
def key_pose(rig, frame, rots, loc=None):
    for pb in rig.pose.bones:
        e = rots.get(pb.name, (0, 0, 0))
        pb.rotation_mode = "XYZ"
        pb.rotation_euler = e
        pb.keyframe_insert("rotation_euler", frame=frame)
    if loc is not None:
        pb = rig.pose.bones["hips"]
        pb.location = loc
        pb.keyframe_insert("location", frame=frame)


def make_action(rig, name):
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    rig.animation_data_create()
    rig.animation_data.action = act
    return act


def build_animations(rig):
    """Bone-local rotations (X = bend forward for limbs pointing down)."""
    arms_down = {"upper_arm.L": (0, 0, 0), "upper_arm.R": (0, 0, 0)}
    # Idle: 2s breathing + subtle sway
    make_action(rig, "Idle")
    for f in range(0, 61, 10):
        s = math.sin(2 * math.pi * f / 60)
        rots = dict(arms_down)
        rots["chest"] = (0.03 * s, 0, 0)
        rots["neck"] = (-0.02 * s, 0.03 * s, 0)
        rots["head"] = (0.02 * s, -0.04 * s, 0.02 * s)
        rots["upper_arm.L"] = (0.03 * s, 0, 0)
        rots["upper_arm.R"] = (0.03 * s, 0, 0)
        rots["forearm.L"] = (0.06 + 0.02 * s, 0, 0)
        rots["forearm.R"] = (0.06 + 0.02 * s, 0, 0)
        key_pose(rig, f + 1, rots, loc=(0, 0.004 * s, 0))
    # Walk: 1s cycle
    make_action(rig, "Walk")
    for f in range(0, 31, 3):
        ph = 2 * math.pi * f / 30
        s, c = math.sin(ph), math.cos(ph)
        rots = {}
        for side, sg in (("L", 1), ("R", -1)):
            sw = s * sg
            rots[f"thigh.{side}"] = (-0.45 * sw, 0, 0)
            knee = max(0.0, math.sin(ph * 1 + (0 if sg > 0 else math.pi) + 1.2))
            rots[f"shin.{side}"] = (0.65 * knee, 0, 0)
            rots[f"foot.{side}"] = (0.15 * sw, 0, 0)
            rots[f"upper_arm.{side}"] = (0.35 * sw, 0, 0)
            rots[f"forearm.{side}"] = (0.25 + 0.15 * max(0, sw), 0, 0)
        rots["spine"] = (0.03, 0.06 * s, 0)
        rots["chest"] = (0, -0.1 * s, 0)
        rots["head"] = (0, 0.05 * s, 0)
        key_pose(rig, f + 1, rots, loc=(0, 0.018 * abs(c) - 0.01, 0))
    rig.animation_data.action = bpy.data.actions["Idle"]
    # push both actions on NLA tracks so the glTF exporter keeps them
    for act in bpy.data.actions:
        tr = rig.animation_data.nla_tracks.new()
        tr.name = act.name
        tr.strips.new(act.name, 1, act)
        tr.mute = True
    rig.animation_data.action = None
    for pb in rig.pose.bones:
        pb.rotation_euler = (0, 0, 0)
        pb.location = (0, 0, 0)


# --------------------------------------------------------------------------
# Render turnaround
# --------------------------------------------------------------------------
def setup_render(res_x=420, res_y=900, samples=48):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    sc.render.resolution_x, sc.render.resolution_y = res_x, res_y
    sc.view_settings.view_transform = "Standard"
    world = bpy.data.worlds.new("World")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.85, 0.87, 0.92, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.7
    for name, rot, energy in (("Key", (0.9, 0.0, -0.6), 2.4), ("Rim", (1.1, 0, 2.6), 1.6)):
        L = bpy.data.lights.new(name, "SUN")
        L.energy = energy
        L.angle = 0.3
        lo = link(bpy.data.objects.new(name, L))
        lo.rotation_euler = rot
    cam = bpy.data.cameras.new("Cam")
    cam.type = "ORTHO"
    cam.ortho_scale = 1.9
    co = link(bpy.data.objects.new("Cam", cam))
    sc.camera = co
    return co


def render_views(cam, prefix="view"):
    # azimuths matching the reference sheet: front, 3/4, profile, back, back 3/4
    views = [("front", 0), ("threequarter", -40), ("side", -90), ("back", 180), ("back_threequarter", -140)]
    paths = []
    for name, az in views:
        a = math.radians(az)
        d = 4.0
        pos = V(math.sin(a) * d, -math.cos(a) * d, 0.93)
        cam.location = pos
        cam.rotation_euler = (math.pi / 2, 0, a)
        p = os.path.join(OUT_RENDER, f"{prefix}_{name}.png")
        bpy.context.scene.render.filepath = p
        bpy.ops.render.render(write_still=True)
        paths.append(p)
    return paths


def compose_sheet(paths, out, ref=None):
    from PIL import Image
    ims = [Image.open(p).convert("RGBA") for p in paths]
    w, h = ims[0].size
    sheet = Image.new("RGBA", (w * len(ims), h), (255, 255, 255, 255))
    for i, im in enumerate(ims):
        sheet.alpha_composite(im, (i * w, 0))
    if ref:
        r = Image.open(ref).convert("RGBA")
        bg = Image.new("RGBA", r.size, (255, 255, 255, 255))
        bg.alpha_composite(r)
        r = bg.resize((sheet.width, int(r.height * sheet.width / r.width)))
        both = Image.new("RGBA", (sheet.width, sheet.height + r.height), (255, 255, 255, 255))
        both.alpha_composite(r, (0, 0))
        both.alpha_composite(sheet, (0, r.height))
        both.convert("RGB").save(out.replace(".png", "_vs_reference.png"))
    sheet.convert("RGB").save(out)


def glb_to_embedded_gltf(glb_path, out_path):
    """Single-file .gltf (buffer as base64 data URI) for web hosts that don't serve .glb."""
    import base64
    import json
    import struct
    data = open(glb_path, "rb").read()
    jlen = struct.unpack_from("<I", data, 12)[0]
    doc = json.loads(data[20:20 + jlen])
    blen = struct.unpack_from("<I", data, 20 + jlen)[0]
    binchunk = data[28 + jlen:28 + jlen + blen]
    doc["buffers"][0]["uri"] = "data:application/octet-stream;base64," + base64.b64encode(binchunk).decode()
    with open(out_path, "w") as f:
        json.dump(doc, f, separators=(",", ":"))


# --------------------------------------------------------------------------
# Wardrobe
# --------------------------------------------------------------------------
WARDROBE = [
    # object name, slot, label, body sections hidden while worn
    ("Outfit_Sweater", "top", "Navy sweater", ["torso", "underwear_top"]),
    ("Outfit_TShirt", "top", "Lavender tee", ["torso", "underwear_top"]),
    ("Outfit_Joggers", "bottom", "Plaid joggers", ["pelvis", "thighs", "shins", "underwear_briefs"]),
    ("Outfit_Shorts", "bottom", "Plaid shorts", ["pelvis", "underwear_briefs"]),
]
DEFAULT_OUTFIT = ["Outfit_Sweater", "Outfit_Joggers"]


def show_outfit(items, sections, worn, render=True):
    hidden = set()
    for name, slot, label, hides in WARDROBE:
        on = name in worn
        items[name].hide_render = not on
        items[name].hide_set(not on)
        if on:
            hidden.update(hides)
    for sec, ob in sections.items():
        ob.hide_render = sec in hidden
        ob.hide_set(sec in hidden)


def export_selection(path, objs, animations):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.hide_set(False)
        o.select_set(True)
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", export_yup=True, use_selection=True,
                              export_animations=animations, export_animation_mode="NLA_TRACKS",
                              export_skins=True, export_extras=True, export_image_format="AUTO")


def set_action(rig, name, frame):
    act = bpy.data.actions.get(name) if name else None
    rig.animation_data.action = act
    if act is not None and getattr(act, "slots", None):
        try:
            rig.animation_data.action_slot = act.slots[0]
        except Exception:
            pass
    bpy.context.scene.frame_set(frame)


def make_hull_hair(old_hair, head, body, mat, rig):
    """Replace the procedural hair with the drawing's visual hull (see hair_hull.py)."""
    import hair_hull as HH
    import reference as REFV
    views = REFV.load_views(include_mirror=True)
    verts, faces = HH.build(views, [head, body], smooth=2.0, reveal_skin=True)
    ob = mesh_obj("Hair", [tuple(v) for v in verts], [tuple(f) for f in faces], mat)
    sm = ob.modifiers.new("Smooth", "SMOOTH")      # melt the voxel terraces
    sm.factor = 0.7
    sm.iterations = 12
    bake(ob)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(ob.data)
    bm.free()
    dec = ob.modifiers.new("Decimate", "DECIMATE")
    dec.ratio = 0.3
    bake(ob)
    shade_smooth(ob)
    for o in old_hair:
        bpy.data.objects.remove(o)
    skin_to(ob, rig, None, rigid="head")
    ob["wardrobe_slot"] = "hair"
    print(f"  hull hair: {len(ob.data.polygons)} faces")
    return ob


def make_hair_shell(hair, mat, voxel=0.0065):
    """One smooth, watertight hair volume (cap + bun + locks) that the drawn strands are painted onto."""
    parts = []
    for o in hair:
        c = o.copy()
        c.data = o.data.copy()
        link(c)
        parts.append(c)
    shell = join(parts, "Hair")
    rm = shell.modifiers.new("Remesh", "REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = voxel
    sm = shell.modifiers.new("Smooth", "SMOOTH")
    sm.factor = 0.6
    sm.iterations = 6
    bake(shell)
    dec = shell.modifiers.new("Decimate", "DECIMATE")
    dec.ratio = 0.35
    bake(shell)
    shade_smooth(shell)
    shell.data.materials.clear()
    shell.data.materials.append(mat)
    for o in hair:
        bpy.data.objects.remove(o)
    return shell


def fit_to_reference(body, face_sec, items, head, ears, hair):
    """Deform the default outfit, visible skin and hair so every reference view's outline matches."""
    sys.path.insert(0, HERE)
    import fit as F
    import reference as REFV
    views = REFV.load_views(include_mirror=True)
    hidden = set()
    for name, slot, label, hides in WARDROBE:
        if name in DEFAULT_OUTFIT:
            hidden.update(hides)
    vis_face = [sec not in hidden and body.data.polygons[i].center.z < HEAD_C.z - 0.06
                for i, sec in enumerate(face_sec)]
    movable = np.zeros(len(body.data.vertices), bool)
    for p in body.data.polygons:
        if vis_face[p.index] and face_sec[p.index] not in ("hands", "feet") and p.center.z < HEAD_C.z - 0.10:
            movable[list(p.vertices)] = True
    C = REFV
    g_skin = ((C.SKIN,), (C.HAIR, C.SHIRT, C.PANTS, C.STRING))
    g_hair = ((C.HAIR,), ())
    g_top = ((C.SHIRT,), (C.HAIR, C.STRING))
    g_bottom = ((C.PANTS, C.STRING), (C.SHIRT,))
    targets = [F.FitTarget(body, face_mask=vis_face, movable=movable, group=g_skin)]
    targets += [F.FitTarget(items[DEFAULT_OUTFIT[0]], group=g_top), F.FitTarget(items[DEFAULT_OUTFIT[1]], group=g_bottom)]
    if not head.get("from_drawing"):
        targets += [F.FitTarget(head, group=g_skin)] + [F.FitTarget(e, group=g_skin) for e in ears]
    targets += [F.FitTarget(h, mode="space", group=g_hair) for h in hair if h.name != "Hair" or "--shell-hair" in sys.argv]
    ious = F.fit(targets, views, iterations=int(os.environ.get("FIT_ITERS", 10)))
    print("FIT IoU", [round(x, 3) for x in ious])
    if not head.get("from_drawing"):
        symmetrize(head)


def symmetrize(ob):
    """Average every vertex with its mirror partner (the drawing's face is symmetric)."""
    from mathutils.kdtree import KDTree
    me = ob.data
    kd = KDTree(len(me.vertices))
    for v in me.vertices:
        kd.insert(v.co, v.index)
    kd.balance()
    co = [v.co.copy() for v in me.vertices]
    for v in me.vertices:
        m = co[v.index].copy()
        m.x = -m.x
        _, j, d = kd.find(m)
        if d < 0.01:
            p = co[j]
            v.co = Vector(((co[v.index].x - p.x) / 2, (co[v.index].y + p.y) / 2, (co[v.index].z + p.z) / 2))
    me.update()


def make_hull_extremities(body, face_sec, ws, rig, mat):
    """Hands and feet as visual hulls of the drawn skin, each carved only from the views that see it."""
    import hair_hull as HH
    import reference as REFV
    views = REFV.load_views(include_mirror=True)
    out = []
    for sx, s in ((1, "L"), (-1, "R")):
        near = "side_mirror" if sx > 0 else "side"
        lo, hi = sorted((sx * 0.15, sx * 0.33))
        specs = [("Hand_" + s, ((lo, hi), (-0.13, 0.12), (0.60, 0.885)), {"forearms", "hands"}),
                 ]
        for name, box, secs in specs:
            verts, faces = HH.build(views, [], voxel=0.0025, views_used=["front", "back", near], dilate=1,
                                    box=box, classes=(REFV.SKIN,), keep_largest=True, smooth=1.6)
            ob = mesh_obj(name, [tuple(v) for v in verts], [tuple(f) for f in faces], mat)
            bm = bmesh.new()
            bm.from_mesh(ob.data)
            bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
            bm.to_mesh(ob.data)
            bm.free()
            dec = ob.modifiers.new("Decimate", "DECIMATE")
            dec.ratio = 0.25
            bake(ob)
            shade_smooth(ob)
            bind_garment(ob, rig, BodySampler(body, face_sec, ws, secs))
            ob["wardrobe_slot"] = "base"
            out.append(ob)
            print(f"  {name}: {len(ob.data.polygons)} faces from the drawing's hull")
    return out


def paint_from_reference(body, face_sec, items, head, ears, hair, extremities=()):
    """Bake the drawn views onto the default outfit, skin and hair as UV textures."""
    import project as PJ
    import reference as REFV
    C = REFV
    views = REFV.load_views(include_mirror=True)
    hidden = set()
    for name, slot, label, hides in WARDROBE:
        if name in DEFAULT_OUTFIT:
            hidden.update(hides)
    top, bottom = items[DEFAULT_OUTFIT[0]], items[DEFAULT_OUTFIT[1]]
    def occluders():
        occ = []
        for ob, faces in [(body, [sec not in hidden for sec in face_sec]), (top, None), (bottom, None), (head, None)] + \
                [(e, None) for e in ears] + [(h, None) for h in hair] + [(x, None) for x in extremities]:
            ob.data.update()
            ob.data.calc_loop_triangles()
            co = np.array([v.co[:] for v in ob.data.vertices])
            idx = np.array([t.vertices[:] for t in ob.data.loop_triangles if faces is None or faces[t.polygon_index]])
            occ.append(co[idx])
        return np.concatenate(occ)
    painter = PJ.Painter(views, occluders(),
                         view_weights={"threequarter": 0.01, "back_threequarter": 0.01})
    jobs = [(head, (C.SKIN, C.HAIR, C.STRING, C.SHIRT, C.PANTS), 1024), (body, (C.SKIN,), 1024), (top, (C.SHIRT,), 1024),
            (bottom, (C.PANTS, C.STRING), 1024)] + [(e, (C.SKIN,), 256) for e in ears] + \
           [(h, (C.HAIR,), 1024 if len(h.data.vertices) > 5000 else 512) for h in hair if not h.name.startswith("HairCards")] + \
           [(x, (C.SKIN,), 512) for x in extremities]
    grown = PJ.grow(head, painter, (C.HAIR,)) if "--shell-hair" in sys.argv else None
    if grown is not None and hair:
        g = grown.vertex_groups.new(name="head")
        g.add(range(len(grown.data.vertices)), 1.0, "REPLACE")
        shell = hair[0]
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        shell.select_set(True)
        grown.select_set(True)
        bpy.context.view_layer.objects.active = shell
        bpy.ops.object.join()
        print(f"  grew hair over {len(shell.data.polygons)} faces total (drawn hair on head skin)")
        painter.zb = {k: PJ.zbuffer(v, occluders()) for k, v in views.items()}
    for rnd in range(10):   # peel layer by layer: a carved surface can reveal another behind it
        n = sum(PJ.carve(h, painter, (C.SKIN,)) for h in hair)
        painter.zb = {k: PJ.zbuffer(v, occluders()) for k, v in views.items()}
        print(f"  carve pass {rnd}: removed {n} hair faces over drawn skin")
        if n == 0:
            break
    if "--no-cards" not in sys.argv:
        hair.extend(make_hair_cards(views, occluders(), head))
    for ob, classes, size in jobs:
        PJ.smart_uv(ob)
        bias = {"side": 0.7, "side_mirror": 0.7, "threequarter": 0.75, "back_threequarter": 0.75} if ob is head else None
        img, frac = painter.paint(ob, classes, size=size, sharp=40.0, bias=bias,
                                  relaxed_classes=(C.SKIN,) if ob is head else None,
                                  min_facing=0.25 if ob is head else (0.45 if ob in hair else 0.3))
        PJ.assign_texture(ob, img, "paint_" + ob.name.lower(), OUT_TEX)
        print(f"  painted {ob.name:18s} {size}px, {frac * 100:.0f}% of texels seen in the drawing")


def make_hair_cards(views, model_tris, head):
    """Alpha cards for the drawn flyaway strands (see cards.py)."""
    import cards as CD
    out = []
    rig = head.parent
    for k, verts, faces, uvs, path, npx in CD.cards(views, model_tris, OUT_TEX, np.array(HEAD_C[:])):
        if not faces:
            continue
        ob = mesh_obj("HairCards_" + k, verts, faces, None, smooth=False)
        uvl = ob.data.uv_layers.new(name="Paint")
        for poly in ob.data.polygons:
            for li in poly.loop_indices:
                uvl.data[li].uv = uvs[ob.data.loops[li].vertex_index]
        im = bpy.data.images.load(path)
        im.pack()
        m = bpy.data.materials.new("HairCards_" + k)
        m.use_nodes = True
        nt = m.node_tree
        b = nt.nodes.get("Principled BSDF")
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = im
        nt.links.new(tex.outputs["Color"], b.inputs["Base Color"])
        nt.links.new(tex.outputs["Color"], b.inputs["Emission Color"])
        # fade cards out as they turn edge-on (they are flat, drawn for one direction)
        geo = nt.nodes.new("ShaderNodeNewGeometry")
        dot = nt.nodes.new("ShaderNodeVectorMath")
        dot.operation = "DOT_PRODUCT"
        nt.links.new(geo.outputs["Normal"], dot.inputs[0])
        nt.links.new(geo.outputs["Incoming"], dot.inputs[1])
        ab = nt.nodes.new("ShaderNodeMath")
        ab.operation = "ABSOLUTE"
        nt.links.new(dot.outputs["Value"], ab.inputs[0])
        mr = nt.nodes.new("ShaderNodeMapRange")
        mr.inputs["From Min"].default_value = 0.80
        mr.inputs["From Max"].default_value = 0.81
        nt.links.new(ab.outputs["Value"], mr.inputs["Value"])
        mul = nt.nodes.new("ShaderNodeMath")
        mul.operation = "MULTIPLY"
        nt.links.new(tex.outputs["Alpha"], mul.inputs[0])
        nt.links.new(mr.outputs["Result"], mul.inputs[1])
        cut = nt.nodes.new("ShaderNodeMath")          # alpha clip: no half-transparent ghosts
        cut.operation = "GREATER_THAN"
        cut.inputs[1].default_value = 0.5
        nt.links.new(mul.outputs["Value"], cut.inputs[0])
        nt.links.new(cut.outputs["Value"], b.inputs["Alpha"])
        b.inputs["Emission Strength"].default_value = 0.0
        b.inputs["Roughness"].default_value = 0.9
        m["painted"] = True
        m["cards"] = True
        try:
            m.surface_render_method = "DITHERED"
        except Exception:
            pass
        ob.data.materials.append(m)
        skin_to(ob, rig, None, rigid="head")
        ob["wardrobe_slot"] = "hair"
        out.append(ob)
        print(f"  hair cards {k}: {len(faces)} cards, {npx} px of drawn wisps")
    return out


def painted_lighting():
    """Render look: the painted colours (which already carry the artist's shading) plus a
    soft real key and rim light on top, and the ink outline."""
    set_paint_emission(0.82)
    for m in bpy.data.materials:
        if m.get("painted") or not m.use_nodes or m.name == "Ink":
            continue
        b = m.node_tree.nodes.get("Principled BSDF")
        if b is None:
            continue
        src = b.inputs["Base Color"]
        if src.links:
            m.node_tree.links.new(src.links[0].from_socket, b.inputs["Emission Color"])
        else:
            b.inputs["Emission Color"].default_value = src.default_value
        b.inputs["Emission Strength"].default_value = 0.6
    for o in bpy.data.objects:
        if o.type == "LIGHT":
            o.data.energy = {"Key": 1.1, "Rim": 0.9}.get(o.name, o.data.energy)
    bpy.context.scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.25
    add_outlines()


def set_paint_emission(strength):
    for m in bpy.data.materials:
        if m.get("painted"):
            b = m.node_tree.nodes.get("Principled BSDF")
            b.inputs["Emission Strength"].default_value = strength


def add_outlines(thickness=0.0032, color="#2B1E22"):
    """Ink outline for renders: an inflated, inside-out copy of each visible mesh whose
    camera-facing side is transparent (the classic inverted-hull toon outline)."""
    m = bpy.data.materials.get("Ink") or bpy.data.materials.new("Ink")
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    mix = nt.nodes.new("ShaderNodeMixShader")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Color"].default_value = srgb(color)
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    nt.links.new(geo.outputs["Backfacing"], mix.inputs["Fac"])
    nt.links.new(em.outputs["Emission"], mix.inputs[1])
    nt.links.new(tr.outputs["BSDF"], mix.inputs[2])
    nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
    for ob in bpy.data.objects:
        if ob.type != "MESH" or ob.name.startswith("Ink_"):
            continue
        if not any(mm and mm.get("painted") for mm in ob.data.materials) or ob.name.startswith("HairCards"):
            continue
        if "Ink" not in [mm.name for mm in ob.data.materials if mm]:
            ob.data.materials.append(m)
        so = ob.modifiers.get("InkOutline") or ob.modifiers.new("InkOutline", "SOLIDIFY")
        so.thickness = thickness
        so.offset = 1.0
        so.use_flip_normals = True
        so.use_rim = False
        so.material_offset = len(ob.data.materials) - 1
        so.show_viewport = True
    bpy.context.scene.cycles.transparent_max_bounces = 16


def render_match(prefix="match"):
    """Render every reference view with a camera that matches it pixel for pixel."""
    import reference as REFV
    from PIL import Image
    views = REFV.load_views(include_mirror=False)
    sc = bpy.context.scene
    cam = sc.camera
    cam.data.type = "ORTHO"
    out = []
    for k, v in views.items():
        sc.render.resolution_x, sc.render.resolution_y = v.w, v.h
        cam.data.ortho_scale = max(v.w, v.h) * v.s
        a = math.radians(v.az)
        R_ = V(math.cos(a), math.sin(a), 0)
        c = R_ * ((v.w / 2 - v.u0) * v.s) + V(0, 0, (v.floor - v.h / 2) * v.s)
        cam.location = c + V(math.sin(a), -math.cos(a), 0) * 5
        cam.rotation_euler = (math.pi / 2, 0, a)
        p = os.path.join(OUT_RENDER, f"{prefix}_{k}.png")
        sc.render.filepath = p
        bpy.ops.render.render(write_still=True)
        ren = Image.open(p).convert("RGBA")
        ref = Image.fromarray(v.rgba)
        bg = Image.new("RGBA", ren.size, (255, 255, 255, 255))
        a_ = bg.copy()
        a_.alpha_composite(ref)
        b_ = bg.copy()
        b_.alpha_composite(ren)
        out.append((a_, b_))
    W = sum(a_.width for a_, _ in out)
    H = out[0][0].height
    sheet = Image.new("RGB", (W, H * 2), (255, 255, 255))
    x = 0
    diffs = []
    for a_, b_ in out:
        sheet.paste(a_.convert("RGB"), (x, 0))
        sheet.paste(b_.convert("RGB"), (x, H))
        diffs.append(np.abs(np.asarray(a_, float)[:, :, :3] - np.asarray(b_, float)[:, :, :3]).mean())
        x += a_.width
    path = os.path.join(OUT_RENDER, f"{prefix}.png")
    sheet.save(path)
    print("MATCH mean abs diff per view:", [round(d, 1) for d in diffs])
    return path


def main():
    import json
    render = "--no-render" not in sys.argv
    reset()
    face_img = face_texture()
    plaid_img = plaid_texture()
    m_skin = material("Skin", COL["skin"], rough=0.6)
    m_under = material("Underwear", COL["underwear"], rough=0.85, spec=0.1)
    m_face = material("Face", COL["skin"], rough=0.6, image=face_img)
    m_shirt = material("Sweater", COL["shirt"], rough=0.95, spec=0.1)
    m_tee = material("Tee", COL["tee"], rough=0.9, spec=0.1)
    m_pants = material("Plaid", COL["pants"], rough=0.95, image=plaid_img, spec=0.1)
    m_hair = material("Hair", COL["hair"], rough=0.55, spec=0.4)
    m_hair_dark = material("HairDark", COL["hair_dark"], rough=0.6, spec=0.3)
    m_string = material("Drawstring", COL["string"], rough=0.9)

    rig = build_armature()
    if "--old-head" in sys.argv:
        head = build_head(m_face)
        ears = build_ears(m_skin)
    else:
        head = build_head_from_drawing(m_skin)
        ears = build_ears_from_drawing(m_skin)
    hair = build_hair(head, m_hair, m_hair_dark)
    if "--shell-hair" in sys.argv:
        hair = [make_hair_shell(hair, m_hair)]
    for o in [head] + ears + hair:
        skin_to(o, rig, None, rigid="head")
        o["wardrobe_slot"] = "hair" if o in hair else "head"

    # base body -> weights -> sections
    body = build_body(m_skin, m_under)
    under = None
    rig_body(body, rig)
    face_sec, face_bone, ws = classify_body(body)
    under = build_underwear(body, face_sec, ws, rig, m_under)

    items = {
        "Outfit_Sweater": build_top("Outfit_Sweater", m_shirt, rig, body, face_sec, ws),
        "Outfit_TShirt": build_top("Outfit_TShirt", m_tee, rig, body, face_sec, ws, ease=0.93, hem=0.925,
                                   sleeve_cut=1.150, sleeve_r=(0.053, 0.047, 0.047, 0.046), neck_depth=0.035),
        "Outfit_Joggers": build_bottom("Outfit_Joggers", m_pants, m_string, rig, body, face_sec, ws),
        "Outfit_Shorts": build_bottom("Outfit_Shorts", m_pants, m_string, rig, body, face_sec, ws, cut=0.640,
                                      leg_ease=1.04),
    }
    for name, slot, label, hides in WARDROBE:
        ob = items[name]
        ob["wardrobe_slot"] = slot
        ob["label"] = label
        ob["hides"] = ",".join(hides)
    if "--strand-hair" not in sys.argv and "--shell-hair" not in sys.argv:
        hair = [make_hull_hair(hair, head, body, m_hair, rig)]
    if "--no-fit" not in sys.argv:
        fit_to_reference(body, face_sec, items, head, ears, hair)
    extremities = []
    if "--no-hull-limbs" not in sys.argv:
        extremities = make_hull_extremities(body, face_sec, ws, rig, m_skin)
    if "--no-paint" not in sys.argv:
        paint_from_reference(body, face_sec, items, head, ears, hair, extremities)
    sections = split_body(body, face_sec)
    sections.update(under)
    bpy.data.objects.remove(body)
    if extremities and "hands" in sections:     # the drawn hands replace the modelled ones
        bpy.data.objects.remove(sections.pop("hands"))
    build_animations(rig)
    show_outfit(items, sections, DEFAULT_OUTFIT)

    blend = os.path.join(OUT_EXPORT, "character.blend")
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.wm.save_as_mainfile(filepath=blend)

    base = [rig, head] + ears + hair + extremities + list(sections.values())
    glb = os.path.join(OUT_EXPORT, "character.glb")
    export_selection(glb, base + list(items.values()), True)
    glb_to_embedded_gltf(glb, os.path.join(OUT_EXPORT, "character.gltf"))
    parts_dir = os.path.join(OUT_EXPORT, "parts")
    os.makedirs(parts_dir, exist_ok=True)
    export_selection(os.path.join(parts_dir, "base_body.glb"), base, True)
    for name, ob in items.items():
        export_selection(os.path.join(parts_dir, name.replace("Outfit_", "").lower() + ".glb"), [rig, ob], False)
    manifest = {
        "skeleton": [b[0] for b in BONES],
        "body_sections": sorted(sections),
        "default_outfit": DEFAULT_OUTFIT,
        "items": [{"node": n, "slot": sl, "label": lb, "hides": h,
                   "file": "parts/" + n.replace("Outfit_", "").lower() + ".glb"} for n, sl, lb, h in WARDROBE],
    }
    with open(os.path.join(OUT_EXPORT, "wardrobe.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    tris = {o.name: sum(len(p.vertices) - 2 for p in o.data.polygons) for o in bpy.data.objects if o.type == "MESH"}
    print(f"EXPORTED {glb} triangles={sum(tris.values())}")
    print({k: v for k, v in sorted(tris.items())})

    if "--match" in sys.argv:
        cam = setup_render()
        rig.data.pose_position = "REST"
        for L in [o for o in bpy.data.objects if o.type == "LIGHT"]:
            L.hide_render = True
        bpy.context.scene.world.node_tree.nodes["Background"].inputs[1].default_value = 0.0
        set_paint_emission(1.0)
        if "--no-outline" not in sys.argv:
            add_outlines()
        render_match()
        return
    if render:
        cam = setup_render()
        rig.data.pose_position = "REST"
        painted_lighting()
        paths = render_views(cam)
        compose_sheet(paths, os.path.join(OUT_RENDER, "turnaround.png"),
                      ref=os.path.join(ROOT, "reference", "turnaround.png"))
        # wardrobe sheet: the same body in each combination
        combos = [("base", []), ("sweater_joggers", ["Outfit_Sweater", "Outfit_Joggers"]),
                  ("tee_shorts", ["Outfit_TShirt", "Outfit_Shorts"]), ("tee_joggers", ["Outfit_TShirt", "Outfit_Joggers"]),
                  ("sweater_shorts", ["Outfit_Sweater", "Outfit_Shorts"])]
        wpaths = []
        a = math.radians(-30)
        for cname, worn in combos:
            show_outfit(items, sections, worn)
            cam.location = V(math.sin(a) * 4, -math.cos(a) * 4, 0.93)
            cam.rotation_euler = (math.pi / 2, 0, a)
            p = os.path.join(OUT_RENDER, f"wardrobe_{cname}.png")
            bpy.context.scene.render.filepath = p
            bpy.ops.render.render(write_still=True)
            wpaths.append(p)
        compose_sheet(wpaths, os.path.join(OUT_RENDER, "wardrobe.png"))
        # deformation check: clothes follow the body mid-stride
        rig.data.pose_position = "POSE"
        dpaths = []
        for cname, worn, frame, az in (("walk_a", DEFAULT_OUTFIT, 9, -90), ("walk_b", DEFAULT_OUTFIT, 9, -35),
                                       ("walk_c", ["Outfit_TShirt", "Outfit_Shorts"], 9, -90),
                                       ("walk_d", ["Outfit_TShirt", "Outfit_Shorts"], 24, -35)):
            show_outfit(items, sections, worn)
            set_action(rig, "Walk", frame)
            a = math.radians(az)
            cam.location = V(math.sin(a) * 4, -math.cos(a) * 4, 0.93)
            cam.rotation_euler = (math.pi / 2, 0, a)
            p = os.path.join(OUT_RENDER, f"deform_{cname}.png")
            bpy.context.scene.render.filepath = p
            bpy.ops.render.render(write_still=True)
            dpaths.append(p)
        compose_sheet(dpaths, os.path.join(OUT_RENDER, "deform.png"))
        set_action(rig, None, 1)
        rig.data.pose_position = "REST"
        show_outfit(items, sections, DEFAULT_OUTFIT)
        cam.data.ortho_scale = 0.42
        for name, az in (("face", 0), ("face_threequarter", -40), ("face_side", -90)):
            a = math.radians(az)
            cam.location = V(math.sin(a) * 4, -math.cos(a) * 4, 1.56)
            cam.rotation_euler = (math.pi / 2, 0, a)
            bpy.context.scene.render.resolution_x = bpy.context.scene.render.resolution_y = 600
            bpy.context.scene.render.filepath = os.path.join(OUT_RENDER, f"closeup_{name}.png")
            bpy.ops.render.render(write_still=True)
        compose_sheet([os.path.join(OUT_RENDER, f"closeup_{n}.png") for n in ("face", "face_threequarter", "face_side")],
                      os.path.join(OUT_RENDER, "closeups.png"))
        print("RENDERED")


if __name__ == "__main__":
    main()
