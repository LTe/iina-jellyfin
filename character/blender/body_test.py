import math
import os
import sys
import time

import bpy

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import anatomy as A  # noqa: E402
import build_character as BC  # noqa: E402
import head_from_drawing as HD  # noqa: E402


def union_body(parts=None, name="Body"):
    if parts is None:
        parts = [A.vertical_loft(A.TORSO), A.vertical_loft(A.NECK, rings=20, m=32)]
        for sx in (1, -1):
            parts.append(A.limb_loft(A.leg_path(sx), A.LEG, sx))
            parts.append(A.limb_loft(A.arm_path(sx), A.ARM, sx, m=32))
            parts += A.foot(sx)
            parts += A.hand(sx)
    V, F = [], []
    for v, f in parts:
        o = len(V)
        V += v
        F += [tuple(i + o for i in ff) for ff in f]
    ob = BC.mesh_obj(name, V, F, None)
    return ob


def remesh(ob, voxel=0.0028, faces=24000, weight_fn=None):
    t = time.time()
    rm = ob.modifiers.new("Remesh", "REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = voxel
    rm.adaptivity = 0.0
    BC.bake(ob)
    cs = ob.modifiers.new("Smooth", "CORRECTIVE_SMOOTH")
    cs.iterations = 8
    cs.smooth_type = "SIMPLE"
    cs.use_only_smooth = True
    BC.bake(ob)
    print("voxel remesh", len(ob.data.polygons), "faces", round(time.time() - t, 1), "s")
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    t = time.time()
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bad = [v for v in bm.verts if not v.is_manifold]
    print("non-manifold verts", len(bad))
    if bad:   # pinch points where two surfaces touch in one voxel: dissolve them, then fill
        bmesh.ops.delete(bm, geom=bad, context="VERTS")
        bmesh.ops.holes_fill(bm, edges=[e for e in bm.edges if e.is_boundary], sides=0)
        bmesh.ops.triangulate(bm, faces=[f for f in bm.faces if len(f.verts) > 4])
    # keep only the biggest shell (voxel remesh can leave tiny inner bubbles) and fix normals
    shells, seen = [], set()
    bm.verts.ensure_lookup_table()
    for v in bm.verts:
        if v.index in seen:
            continue
        stack, comp = [v], []
        seen.add(v.index)
        while stack:
            x = stack.pop()
            comp.append(x)
            for e in x.link_edges:
                y = e.other_vert(x)
                if y.index not in seen:
                    seen.add(y.index)
                    stack.append(y)
        shells.append(comp)
    shells.sort(key=len, reverse=True)
    print("shells", [len(c) for c in shells[:5]], "bad edges", sum(1 for e in bm.edges if not e.is_manifold), "degenerate", sum(1 for f in bm.faces if f.calc_area() < 1e-12))
    for comp in shells[1:]:
        bmesh.ops.delete(bm, geom=comp, context="VERTS")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.validate()
    ob.data.update()
    bpy.context.view_layer.update()
    r = {"CANCELLED"}
    for sym in (True, False):
        try:
            r = bpy.ops.object.quadriflow_remesh(target_faces=faces, use_preserve_sharp=False,
                                                 use_preserve_boundary=False, use_mesh_symmetry=sym, smooth_normals=True)
        except Exception as ex:
            print("quadriflow failed", ex)
        if "FINISHED" in r:
            break
    print("quadriflow", r, len(ob.data.polygons), "faces", round(time.time() - t, 1), "s")
    if "FINISHED" not in r:     # fall back: evenly decimated voxel mesh
        d = ob.modifiers.new("Dec", "DECIMATE")
        d.ratio = faces * 2 / max(1, len(ob.data.polygons))
        BC.bake(ob)
    blend_junctions(ob, weight_fn=weight_fn)
    BC.shade_smooth(ob)
    return ob


def blend_junctions(ob, iterations=60, weight_fn=None):
    weight_fn = weight_fn or A.junction_weight
    """Smooth only around the joins so the union creases melt into soft transitions."""
    g = ob.vertex_groups.new(name="junction")
    n = 0
    for v in ob.data.vertices:
        w = weight_fn(v.co)
        if w > 0:
            n += 1
            g.add([v.index], w, "REPLACE")
    m = ob.modifiers.new("Blend", "SMOOTH")
    m.factor = 0.9
    m.iterations = iterations
    m.vertex_group = "junction"
    before = [v.co.copy() for v in ob.data.vertices]
    BC.bake(ob)
    moved = max((a - b.co).length for a, b in zip(before, ob.data.vertices))
    print("junction blend:", n, "verts weighted, max move", round(moved, 4))
    ob.vertex_groups.remove(ob.vertex_groups["junction"])


def main():
    BC.reset()
    body = remesh(union_body())
    verts, faces = HD.build_mesh()
    head = BC.mesh_obj("Head", verts, faces, None)
    m = bpy.data.materials.new("clay")
    m.use_nodes = True
    m.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.8, 0.62, 0.52, 1)
    for o in (body, head):
        o.data.materials.append(m)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = 24
    sc.cycles.use_denoising = True
    sc.render.film_transparent = True
    sc.render.resolution_x, sc.render.resolution_y = 420, 1000
    w = bpy.data.worlds.new("w")
    sc.world = w
    w.use_nodes = True
    w.node_tree.nodes["Background"].inputs[1].default_value = 0.5
    L = bpy.data.lights.new("k", "SUN")
    L.energy = 3
    lo = bpy.data.objects.new("k", L)
    sc.collection.objects.link(lo)
    lo.rotation_euler = (0.8, 0, -0.6)
    cam = bpy.data.cameras.new("c")
    cam.type = "ORTHO"
    cam.ortho_scale = 1.8
    co = bpy.data.objects.new("c", cam)
    sc.collection.objects.link(co)
    sc.camera = co
    from PIL import Image
    ims = []
    for az in (0, -35, -90, 180):
        a = math.radians(az)
        co.location = (math.sin(a) * 4, -math.cos(a) * 4, 0.86)
        co.rotation_euler = (math.pi / 2, 0, a)
        p = f"/tmp/bt_{az}_{int(time.time())}.png"
        sc.render.filepath = p
        bpy.ops.render.render(write_still=True)
        ims.append(Image.open(p).convert("RGBA"))
    s = Image.new("RGBA", (420 * len(ims), 1000), "white")
    for i, im in enumerate(ims):
        s.alpha_composite(im, (i * 420, 0))
    s.convert("RGB").save(sys.argv[-1])
    bpy.ops.wm.save_as_mainfile(filepath="/tmp/body_test.blend")


if __name__ == "__main__":
    main()
