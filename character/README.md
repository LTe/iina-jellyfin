# Character model: "Pajama Girl"

A rigged, game-ready 3D model of the character in `reference/turnaround.png`, generated procedurally with Blender's Python API.

## v3: AI shape painted with the original art (current)

```sh
pip install gradio_client
cd character/ai && python3 run_hy.py mv2 /shape_generation tencent/Hunyuan3D-2mv   # free HF Space, 4 views
python3 character/ai/texture_hy.py -- character/ai/hy_mv2_shape_generation_0.glb character/ai/character_ai.glb
python3 character/ai/rig_ai.py -- character/ai/character_ai.glb character/export/character.glb
```

- **Shape:** Hunyuan3D-2mv (open source, running on a free Hugging Face Space) generates the mesh from the front, back and both side views of the sheet.
- **Texture:** `texture_hy.py` aligns the mesh to the calibrated views (85–88% silhouette overlap) and paints it by projecting the drawing. The head is painted from the front view only, so there is one face.
- **Rig:** `rig_ai.py` copies weights from the anatomical v2 body (Data Transfer), limits each part to the bones that should move it, splits the mesh into Hair, Body, Sweater and Joggers by painted colour, and adds the Idle and Walk clips.
- **Known limit:** the generated mesh fuses the hands to the hips, so arm swing is kept small.

## v2: built from anatomy

```sh
python3 character/blender/build_v2.py
```

- `anatomy.py` holds the proportion research and every body section. The body uses real female landmarks (7.15 heads tall, crotch at half height, elbows at the waist, wrists at the crotch, hips as wide as the shoulders) plus deliberate anime changes: a larger head, a thin long neck, slim limbs, small hands and feet, and big low-set eyes with a minimal nose and mouth.
- The body parts are lofted from superellipse sections, unioned with Blender's voxel remesh, blended at the joins and decimated, then weighted with bone heat.
- The sweater and joggers are built from the same skeleton paths and sections plus ease. They are cut with planar bisects, weighted with Data Transfer from the body, and each is limited to the bones that should move it.
- Hair is its own mesh: a continuous volume plus strand clumps and a bun.

## Build

The main build is the anime pipeline:

```sh
python3 character/blender/build_anime.py             # build, export, EEVEE toon renders
python3 character/blender/build_anime.py --no-render
```

EEVEE needs `libegl1` installed when running headless. The older reference-projection build is kept as `build_character.py`; the anime build reuses its body, rig, garments and export code.

## Anime pipeline (`build_anime.py`)

- **Face:** the head is modelled from the drawn outline and profile (`head_from_drawing.py`). The eyes, brows, lashes, mouth and blush are one vector-drawn decal at the positions measured on the sheet, projected from the front only. Normals are transferred from a smooth proxy so cel shadows stay clean.
- **Hair (separate mesh and slot):** a continuous hair volume whose edge lies exactly on the hairline, 64 tapered, lens-profiled strand clumps that each run unbroken from the hairline into the bun, a bun wound from thick clumps, face-framing locks and nape wisps. A strand texture adds clump-edge shading and a highlight band.
- **Clothes (separate meshes):** the sweater is one skin-modifier graph (torso plus sleeves, so the shoulders are seamless), with the neckline, hem and sleeve ends cut cleanly. Joggers, tee and shorts work the same way.
- **Shading:** exported glTF materials are plain PBR (base colour and textures). Renders rebuild them as a 2-tone cel shader (Diffuse, Shader to RGB, constant ramp, rim light) with inverted-hull ink outlines.

Older build:

```sh
pip install bpy pillow numpy        # Blender 5.0 as a Python module
python3 character/blender/build_character.py            # build, export, render
python3 character/blender/build_character.py --no-render
```

## Outputs

| Path | What |
| --- | --- |
| `export/character.glb` | Everything in one file: rig (21 bones), base body, head, hair, all clothing items, `Idle` and `Walk` clips. Meters, Y-up, facing +Z |
| `export/parts/base_body.glb` | Rig, body (split into hideable sections), head, hair, underwear and the animations |
| `export/parts/hair_bun.glb` | The hair, skinned to the head bone |
| `export/parts/{sweater,tshirt,joggers,shorts}.glb` | One clothing item each, skinned to the same skeleton |
| `export/wardrobe.json` | Slots, labels and the body sections each item hides while worn |
| `export/character.gltf` | The same asset as a single embedded-JSON glTF |
| `export/character.blend` | Blender scene |
| `renders/` | Cycles turnaround renders, a comparison against the reference, face close-ups |
| `textures/` | Generated face and plaid textures |
| `viewer/index.html` | three.js viewer (serve `character/` over HTTP and open `viewer/`) |

## Matching the reference sheet

The model is built to be indistinguishable from `reference/turnaround.png` in the drawn views:

- `reference.py` calibrates each drawn view as an orthographic camera (meters per pixel, centre line, azimuth from `calibration.json`, solved by `calibrate.py`) and labels every pixel as hair, skin, sweater, pants or drawstring.
- `fit.py` deforms the body and clothes until their outline in every view matches the drawn outline of the same part (about 93% overlap per view).
- `head_from_drawing.py` models the head from the traced face outline (front) and profile (side): brow, nose, lips and chin are real geometry.
- `hair_hull.py` builds the hair as the visual hull of the drawn hair in all views. Hands use the same technique. `cards.py` adds alpha "cards" for the flyaway strands the solid hull can't hold.
- `project.py` bakes the drawing into UV textures: each texel takes its colour from the view that sees it most directly, checking visibility and that the drawing shows the same part there.

`python3 character/blender/build_character.py --match` renders every drawn view with a matched camera into `renders/match.png` (top: sheet, bottom: model).

## Wardrobe

Clothes are separate items that share the body's skeleton:

1. **Base body.** One continuous mesh from neck to toes, weighted with Blender's automatic bone-heat weights, then split into 9 sections (neck, torso, upperarms, forearms, hands, pelvis, thighs, shins, feet). Custom normals keep the section seams invisible.
2. **Fit.** Each garment is built from the body's skeleton with ease added and cut to length. Any vertex that would sit inside the skin is pushed 6 mm outside it (`fit_outside`).
3. **Bind.** Garment weights are copied from the nearest body surface, limited to the relevant region (sleeves from the arms only, pants from the pelvis and legs), so fabric bends like the skin (`bind_garment`).
4. **Hide.** Each item lists the body sections it fully covers. The game hides those sections while the item is worn, which saves overdraw and stops skin poking through.

In an engine, load `base_body.glb` once and bind each item's mesh to the body's skeleton by bone name. That is Unity's shared-bones pattern, Unreal's Leader Pose component, or Godot's skeleton path. To add a new item, write a `build_*` call in `main()` and a row in `WARDROBE`.

All shapes, colors and proportions are parameters at the top of each `build_*` function, so iterating on the model means editing numbers and re-running the script.
