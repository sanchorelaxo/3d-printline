#!/usr/bin/env python3
"""
Blender Python script for mesh decimation and STL export.
Runs headless inside Docker: blender -b -P decimate_and_export.py -- [args]

Supports OBJ and GLB/GLTF input, exports decimated STL.
"""
import bpy
import sys
import os
import argparse


def parse_args():
    # Blender passes everything after '--' to sys.argv
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    parser = argparse.ArgumentParser(description="Decimate and export mesh")
    parser.add_argument("--inm", required=True, help="Input model path (OBJ, GLB, GLTF)")
    parser.add_argument("--outm", required=True, help="Output STL path")
    parser.add_argument("--ratio", type=float, default=0.5,
                        help="Decimation ratio (0.0-1.0, default 0.5)")
    parser.add_argument("--nfaces", type=int, default=None,
                        help="Target number of faces (overrides --ratio)")
    return parser.parse_args(argv)


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for obj in bpy.data.objects:
        bpy.data.objects.remove(obj, do_unlink=True)


def import_model(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".obj":
        # Blender 3.x uses wm.obj_import, 2.x uses import_scene.obj
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=filepath)
        else:
            bpy.ops.import_scene.obj(filepath=filepath)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=filepath)
    elif ext == ".stl":
        bpy.ops.import_mesh.stl(filepath=filepath)
    elif ext == ".ply":
        bpy.ops.import_mesh.ply(filepath=filepath)
    else:
        raise ValueError(f"Unsupported format: {ext}")


def join_all_meshes():
    mesh_objects = [obj for obj in bpy.data.objects if obj.type == 'MESH']
    if not mesh_objects:
        raise RuntimeError("No mesh objects found after import")

    bpy.ops.object.select_all(action='DESELECT')
    for obj in mesh_objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = mesh_objects[0]

    if len(mesh_objects) > 1:
        bpy.ops.object.join()

    return bpy.context.active_object


def decimate(obj, ratio=None, target_faces=None):
    original_faces = len(obj.data.polygons)
    print(f"Original face count: {original_faces}")

    if target_faces is not None:
        if target_faces >= original_faces:
            print(f"Target faces ({target_faces}) >= original ({original_faces}), skipping decimation")
            return
        ratio = target_faces / original_faces

    print(f"Decimation ratio: {ratio}")

    modifier = obj.modifiers.new(name="Decimate", type='DECIMATE')
    modifier.ratio = ratio
    modifier.use_collapse_triangulate = True

    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.modifier_apply(modifier=modifier.name)

    final_faces = len(obj.data.polygons)
    print(f"Final face count: {final_faces} (reduced by {100 * (1 - final_faces / original_faces):.1f}%)")


def export_stl(filepath):
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    bpy.ops.export_mesh.stl(filepath=filepath, use_selection=True)
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    print(f"Exported STL: {filepath} ({size_mb:.1f} MB)")


def main():
    args = parse_args()

    print(f"Input:  {args.inm}")
    print(f"Output: {args.outm}")

    clear_scene()
    import_model(args.inm)
    obj = join_all_meshes()

    bpy.ops.object.select_all(action='DESELECT')
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

    decimate(obj, ratio=args.ratio, target_faces=args.nfaces)
    export_stl(args.outm)

    print("Done.")


if __name__ == "__main__":
    main()
