# Character model: "Pajama Girl"

A rigged, game-ready 3D model of the character in `reference/turnaround.png`, generated procedurally with Blender's Python API.

## Build

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
| `export/parts/{sweater,tshirt,joggers,shorts}.glb` | One clothing item each, skinned to the same skeleton |
| `export/wardrobe.json` | Slots, labels and the body sections each item hides while worn |
| `export/character.gltf` | The same asset as a single embedded-JSON glTF |
| `export/character.blend` | Blender scene |
| `renders/` | Cycles turnaround renders, a comparison against the reference, face close-ups |
| `textures/` | Generated face and plaid textures |
| `viewer/index.html` | three.js viewer (serve `character/` over HTTP and open `viewer/`) |

## Wardrobe

Clothes are separate items that share the body's skeleton:

1. **Base body.** One continuous mesh from neck to toes, weighted with Blender's automatic bone-heat weights, then split into 9 sections (neck, torso, upperarms, forearms, hands, pelvis, thighs, shins, feet). Custom normals keep the section seams invisible.
2. **Fit.** Each garment is built from the body's skeleton with ease added and cut to length. Any vertex that would sit inside the skin is pushed 6 mm outside it (`fit_outside`).
3. **Bind.** Garment weights are copied from the nearest body surface, limited to the relevant region (sleeves from the arms only, pants from the pelvis and legs), so fabric bends like the skin (`bind_garment`).
4. **Hide.** Each item lists the body sections it fully covers. The game hides those sections while the item is worn, which saves overdraw and stops skin poking through.

In an engine, load `base_body.glb` once and bind each item's mesh to the body's skeleton by bone name. That is Unity's shared-bones pattern, Unreal's Leader Pose component, or Godot's skeleton path. To add a new item, write a `build_*` call in `main()` and a row in `WARDROBE`.

All shapes, colors and proportions are parameters at the top of each `build_*` function, so iterating on the model means editing numbers and re-running the script.
