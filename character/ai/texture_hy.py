"""Hunyuan3D mesh -> aligned to the turnaround sheet -> painted from the drawing.

    python3 character/ai/texture_hy.py <input.glb> <out.glb>
"""
import math
import os
import sys

import bpy
import bmesh
import numpy as np
from mathutils import Vector, Matrix

HERE = os.path.dirname(os.path.abspath(__file__))
BL = os.path.join(os.path.dirname(HERE), "blender")
sys.path.insert(0, BL)
import reference as R  # noqa: E402
import project as PJ  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:]
src, dst = args[0], args[1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
for o in bpy.context.view_layer.objects:
    o.select_set(o in meshes)
bpy.context.view_layer.objects.active = meshes[0]
if len(meshes) > 1:
    bpy.ops.object.join()
ob = bpy.context.view_layer.objects.active
bpy.ops.object.parent_clear(type="CLEAR_KEEP_TRANSFORM")
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
ob.name = "Character"

# ---- normalise: feet on the floor, 1.76 m tall, centred
co = np.array([v.co[:] for v in ob.data.vertices])
lo, hi = co.min(0), co.max(0)
s = R.HEIGHT / (hi[2] - lo[2])
feet = co[co[:, 2] < lo[2] + 0.05 * (hi[2] - lo[2])]
cx, cy = feet[:, 0].mean(), feet[:, 1].mean()
M = Matrix.Scale(s, 4) @ Matrix.Translation(Vector((-cx, -cy, -lo[2])))
ob.data.transform(M)
ob.data.update()

# ---- decimate to a game-friendly density before any heavy work
dec = ob.modifiers.new("Dec", "DECIMATE")
target = int(os.environ.get("TARGET_FACES", 120000))
dec.ratio = min(1.0, target / len(ob.data.polygons))
bpy.ops.object.modifier_apply(modifier=dec.name)
print("faces", len(ob.data.polygons))

# ---- fine alignment against the drawn silhouettes (x/y shift, small scale)
views = R.load_views(include_mirror=True)


def tris_of(ob):
    ob.data.calc_loop_triangles()
    c = np.array([v.co[:] for v in ob.data.vertices])
    idx = np.array([t.vertices[:] for t in ob.data.loop_triangles])
    return c[idx]


T0 = tris_of(ob)
sub = T0[:: max(1, len(T0) // 40000)]


def score(dx, dy, k, rot):
    c, sn = math.cos(rot), math.sin(rot)
    Rm = np.array([[c, -sn, 0], [sn, c, 0], [0, 0, 1]])
    T = (sub.reshape(-1, 3) @ Rm.T) * [k, k, k] + [dx, dy, 0]
    T = T.reshape(-1, 3, 3)
    return np.mean([R.iou(R.rasterize_mask(views[n], T), views[n].mask) for n in ("front", "side", "back")])


best = (score(0, 0, 1, 0), 0.0, 0.0, 1.0, 0.0)
print("start IoU", round(best[0], 4))
for step in (0.02, 0.008, 0.003):
    improved = True
    while improved:
        improved = False
        _, dx, dy, k, rot = best
        for cand in ((dx + step, dy, k, rot), (dx - step, dy, k, rot), (dx, dy + step, k, rot), (dx, dy - step, k, rot),
                     (dx, dy, k * (1 + step), rot), (dx, dy, k * (1 - step), rot),
                     (dx, dy, k, rot + step * 2), (dx, dy, k, rot - step * 2)):
            sc_ = score(*cand)
            if sc_ > best[0] + 1e-4:
                best = (sc_,) + cand
                improved = True
print("aligned IoU", round(best[0], 4), "shift", best[1:3], "scale", best[3], "yaw", best[4])
_, dx, dy, k, rot = best
ob.data.transform(Matrix.Translation(Vector((dx, dy, 0))) @ Matrix.Rotation(rot, 4, "Z") @ Matrix.Scale(k, 4))
ob.data.update()
for n in views:
    if n != "side_mirror":
        print(f"  {n:18s} IoU {R.iou(R.rasterize_mask(views[n], tris_of(ob)), views[n].mask):.3f}")

# ---- paint from the drawing: every texel takes the colour of the view that sees it best
PJ.smart_uv(ob)
painter = PJ.Painter(views, tris_of(ob), view_weights={"threequarter": 0.35, "back_threequarter": 0.35})
painter.bias = {"side": 0.9, "side_mirror": 0.9, "threequarter": 1.0, "back_threequarter": 1.0}
size = int(os.environ.get("TEX", 4096))
img, frac = painter.paint(ob, (R.HAIR, R.SKIN, R.SHIRT, R.PANTS, R.STRING), size=size, sharp=24.0, min_facing=0.12)
print(f"painted {frac * 100:.0f}% of texels from the drawing")
# head: one face only. Front view owns everything facing forward; sides only paint the
# ears/temples; the 3/4 drawings (which disagree with the front about where the eyes are) are left out.
def ndimage_erode(m):
    from scipy import ndimage
    return ndimage.binary_erosion(m, iterations=4)


head_views = {k: v for k, v in views.items() if k in ("front", "side", "side_mirror", "back")}
hp = PJ.Painter(head_views, tris_of(ob))
hp.bias = {"side": 0.85, "side_mirror": 0.85, "back": 1.0, "front": 1.0}
CL = (R.HAIR, R.SKIN, R.SHIRT, R.PANTS, R.STRING)
for k in ("side", "side_mirror"):
    v = head_views[k]
    m0 = hp.class_mask(k, CL).copy()
    u = np.arange(v.w)
    ycol = np.asarray(v.R)[1] * (u - v.u0) * v.s          # world Y of each image column
    rows = np.arange(v.h)
    zrow = (v.floor - rows) * v.s
    behind = (ycol[None, :] > -0.045) | (zrow[:, None] < 1.40)   # ear, back of head, neck/body
    hair = R.classify(v) == R.HAIR
    hp._cls_cache[(k, tuple(CL))] = m0 & (behind | hair)
fv = views["front"]
skin_px = fv.rgba[:, :, :3][ndimage_erode(R.classify(fv) == R.SKIN)]
skin_rgb = np.median(skin_px, 0)
print("fallback skin colour", skin_rgb)
img_h, _ = hp.paint(ob, (R.HAIR, R.SKIN, R.SHIRT, R.PANTS, R.STRING), size=size, sharp=40.0, min_facing=0.12,
                    fallback=skin_rgb)
px, Pt, Nt = PJ.texels(ob, size)
w = np.zeros((size, size))
zz = np.clip((Pt[:, 2] - 1.36) / 0.04, 0, 1)            # blend in over 4 cm below the chin
w[px[:, 1], px[:, 0]] = zz
from scipy import ndimage
w = ndimage.grey_dilation(w, size=3)[..., None]
img = img * (1 - w) + img_h * w
m = PJ.assign_texture(ob, img, "character_paint", os.path.join(os.path.dirname(HERE), "textures"))
b = m.node_tree.nodes["Principled BSDF"]
b.inputs["Roughness"].default_value = 0.85
for p in ob.data.polygons:
    p.use_smooth = True
bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", export_yup=True, use_selection=False,
                          export_image_format="JPEG", export_jpeg_quality=90)
bpy.context.preferences.filepaths.save_version = 0
bpy.ops.wm.save_as_mainfile(filepath=dst.replace(".glb", ".blend"))
print("WROTE", dst)
