import bpy, math, sys
from mathutils import Vector
args = [a for a in sys.argv[sys.argv.index('--') + 1:] if not a.startswith('--')]
glb, out = args[0], args[1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
ctr, size = (lo + hi) / 2, max(hi - lo)
print('bbox', lo, hi)
clay = '--clay' in sys.argv
if clay:
    m = bpy.data.materials.new('c'); m.use_nodes = True
    m.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.8, 0.65, 0.55, 1)
    for o in meshes:
        o.data.materials.clear(); o.data.materials.append(m)
if '--emit' in sys.argv:
    for o in meshes:
        for m in o.data.materials:
            b = m.node_tree.nodes.get('Principled BSDF')
            t = [n for n in m.node_tree.nodes if n.type == 'TEX_IMAGE']
            if b and t:
                m.node_tree.links.new(t[0].outputs['Color'], b.inputs['Emission Color'])
                b.inputs['Emission Strength'].default_value = 0.75
sc = bpy.context.scene
sc.render.engine = 'CYCLES'; sc.cycles.samples = 24; sc.cycles.use_denoising = True
sc.render.film_transparent = True; sc.render.resolution_x, sc.render.resolution_y = 400, 800
w = bpy.data.worlds.new('w'); sc.world = w; w.use_nodes = True; w.node_tree.nodes['Background'].inputs[1].default_value = 1.0
L = bpy.data.lights.new('k', 'SUN'); L.energy = 2.5; lo_ = bpy.data.objects.new('k', L); sc.collection.objects.link(lo_); lo_.rotation_euler = (0.9, 0, -0.6)
cam = bpy.data.cameras.new('c'); cam.type = 'ORTHO'; cam.ortho_scale = size * 1.08
co = bpy.data.objects.new('c', cam); sc.collection.objects.link(co); sc.camera = co
up = 'Z'
from PIL import Image
ims = []
for az in (0, 45, 90, 180, 270):
    a = math.radians(az)
    co.location = ctr + Vector((math.sin(a) * 5, -math.cos(a) * 5, 0))
    co.rotation_euler = (math.pi / 2, 0, a)
    p = f'/tmp/vg_{az}.png'; sc.render.filepath = p; bpy.ops.render.render(write_still=True)
    ims.append(Image.open(p).convert('RGBA'))
s = Image.new('RGBA', (400 * len(ims), 800), 'white')
for i, im in enumerate(ims): s.alpha_composite(im, (i * 400, 0))
s.convert('RGB').save(out)
