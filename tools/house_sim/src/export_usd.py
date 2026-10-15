"""Export the canonical mesh scene; run with installed Isaac Python (no GUI)."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import zipfile
from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdPhysics, UsdShade, Vt

ROOT = Path(__file__).resolve().parents[1]


def linear(value):
    return value / 12.92 if value <= .04045 else ((value+.055)/1.055)**2.4


def export_scene(scene, output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(output))
    UsdGeom.SetStageMetersPerUnit(stage, 1.)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    world = UsdGeom.Xform.Define(stage, '/World')
    stage.SetDefaultPrim(world.GetPrim())
    stage.GetRootLayer().customLayerData = {
        'sceneRevision': scene['revision'], 'sourceFrame': scene['frame'],
        'navigationReady': False, 'canonicalScene': 'data/scene.json',
        'geometry': 'same vertices, normals, UVs and object transforms as web'}
    UsdPhysics.Scene.Define(stage, '/World/PhysicsScene')
    materials = {}
    dependencies = []
    for key, spec in scene['materials'].items():
        path = '/World/Materials/' + key
        material = UsdShade.Material.Define(stage,path)
        shader = UsdShade.Shader.Define(stage,path+'/Surface')
        shader.CreateIdAttr('UsdPreviewSurface')
        shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*[linear(v) for v in spec.get('color',[.8]*3)]))
        shader.CreateInput('roughness',Sdf.ValueTypeNames.Float).Set(spec.get('roughness',.65))
        shader.CreateInput('metallic',Sdf.ValueTypeNames.Float).Set(spec.get('metalness',0.))
        shader.CreateInput('opacity',Sdf.ValueTypeNames.Float).Set(spec.get('opacity',1.))
        if spec.get('emissive'):
            shader.CreateInput('emissiveColor',Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*spec['emissive']))
        if spec.get('texture'):
            relative=Path(spec['texture'])
            source=(ROOT/'data'/relative).resolve()
            if not source.is_relative_to(ROOT/'data') or not source.is_file():
                raise ValueError(f'Missing/outside texture {relative}')
            destination=output.parent/relative
            destination.parent.mkdir(parents=True,exist_ok=True)
            if source != destination:
                shutil.copyfile(source,destination)
            dependencies.append(relative.as_posix())
            st=UsdShade.Shader.Define(stage,path+'/ST');st.CreateIdAttr('UsdPrimvarReader_float2')
            st.CreateInput('varname',Sdf.ValueTypeNames.Token).Set('st')
            tex=UsdShade.Shader.Define(stage,path+'/Texture');tex.CreateIdAttr('UsdUVTexture')
            tex.CreateInput('file',Sdf.ValueTypeNames.Asset).Set(relative.as_posix())
            tex.CreateInput('sourceColorSpace',Sdf.ValueTypeNames.Token).Set('sRGB')
            tex.CreateInput('wrapS',Sdf.ValueTypeNames.Token).Set('repeat')
            tex.CreateInput('wrapT',Sdf.ValueTypeNames.Token).Set('repeat')
            tex.CreateInput('st',Sdf.ValueTypeNames.Float2).ConnectToSource(st.ConnectableAPI(),'result')
            tex.CreateOutput('rgb',Sdf.ValueTypeNames.Float3)
            shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(),'rgb')
        material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(),'surface')
        materials[key]=material
    vertex_count=face_count=colliders=0
    for obj in scene['objects']:
        name=obj['id']; path='/World/Objects/'+name
        group=UsdGeom.Xform.Define(stage,path)
        group.AddTranslateOp().Set(Gf.Vec3d(*obj['position']))
        group.AddRotateZOp().Set(float(obj.get('yaw',0)))
        group.GetPrim().SetCustomDataByKey('label',obj['label'])
        group.GetPrim().SetCustomDataByKey('editable',obj['editable'])
        group.GetPrim().SetCustomDataByKey('evidence',json.dumps(obj.get('evidence',[]),ensure_ascii=False))
        group.GetPrim().SetCustomDataByKey('uncertainty',obj.get('uncertainty',''))
        if not obj.get('visible',True):
            group.MakeInvisible()
        for i,part in enumerate(obj['parts']):
            m=UsdGeom.Mesh.Define(stage,f'{path}/part_{i:03d}')
            m.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in part['vertices']]))
            m.CreateFaceVertexCountsAttr([3]*len(part['triangles']))
            m.CreateFaceVertexIndicesAttr([v for f in part['triangles'] for v in f])
            m.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in part['normals']]))
            m.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
            m.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
            m.CreateDoubleSidedAttr(True)
            uv=UsdGeom.PrimvarsAPI(m).CreatePrimvar('st',Sdf.ValueTypeNames.TexCoord2fArray,UsdGeom.Tokens.vertex)
            repeat=scene['materials'][part['material']].get('repeat',[1,1])
            uv.Set(Vt.Vec2fArray([Gf.Vec2f(p[0]*repeat[0],p[1]*repeat[1]) for p in part['uvs']]))
            UsdShade.MaterialBindingAPI.Apply(m.GetPrim()).Bind(materials[part['material']])
            if part.get('collision',False) and obj.get('visible',True):
                UsdPhysics.CollisionAPI.Apply(m.GetPrim())
                UsdPhysics.MeshCollisionAPI.Apply(m.GetPrim()).CreateApproximationAttr('none')
                colliders+=1
            vertex_count+=len(part['vertices']);face_count+=len(part['triangles'])
    # A neutral environment plus area luminaires at observed ceiling locations.
    dome=UsdLux.DomeLight.Define(stage,'/World/Lighting/Ambient')
    dome.CreateIntensityAttr(350)
    for i,position in enumerate([[5.25,2.9,2.20],[1.2,4.45,2.20],[3.9,5.5,2.20],[8.25,3.35,2.20],[6.65,5.25,2.20]]):
        light=UsdLux.RectLight.Define(stage,f'/World/Lighting/Ceiling_{i}')
        UsdGeom.Xformable(light).AddTranslateOp().Set(Gf.Vec3d(*position))
        light.CreateWidthAttr(.8);light.CreateHeightAttr(.32);light.CreateIntensityAttr(30000)
        light.CreateColorAttr(Gf.Vec3f(1.,.97,.92))
    for record in scene['cameras']:
        camera=UsdGeom.Camera.Define(stage,'/World/Cameras/'+record['id'])
        if record.get('world_from_camera'):
            # Gf matrices use row vectors; JSON/OpenGL uses column vectors.
            rows=record['world_from_camera']
            matrix=Gf.Matrix4d(*[rows[j][i] for i in range(4) for j in range(4)])
        else:
            matrix=Gf.Matrix4d().SetLookAt(Gf.Vec3d(*record['position']),Gf.Vec3d(*record['target']),Gf.Vec3d(*record.get('up',[0,0,1]))).GetInverse()
        UsdGeom.Xformable(camera).AddTransformOp().Set(matrix)
        intrinsics=record.get('intrinsics')
        if intrinsics:
            focal=20.
            camera.CreateFocalLengthAttr(focal)
            camera.CreateHorizontalApertureAttr(focal*intrinsics['width']/intrinsics['fx'])
            camera.CreateVerticalApertureAttr(focal*intrinsics['height']/intrinsics['fy'])
            camera.CreateHorizontalApertureOffsetAttr(focal*(intrinsics['width']/2-intrinsics['cx'])/intrinsics['fx'])
            camera.CreateVerticalApertureOffsetAttr(focal*(intrinsics['cy']-intrinsics['height']/2)/intrinsics['fy'])
        else:
            aperture=24.;focal=aperture/(2*math.tan(math.radians(record.get('fov_y_deg',55))/2))
            camera.CreateFocalLengthAttr(focal);camera.CreateVerticalApertureAttr(aperture)
            camera.CreateHorizontalApertureAttr(aperture*record.get('aspect',1.6))
        camera.CreateClippingRangeAttr(Gf.Vec2f(.03,100))
        camera.GetPrim().SetCustomDataByKey('alignmentStatus',record.get('alignment_status','authored_view'))
    stage.GetRootLayer().Save()
    reopened=Usd.Stage.Open(str(output))
    if reopened.GetRootLayer().customLayerData['sceneRevision'] != scene['revision']:
        raise RuntimeError('USD revision mismatch')
    return dict(revision=scene['revision'],objects=len(scene['objects']),vertices=vertex_count,
                triangles=face_count,colliders=colliders,textures=sorted(set(dependencies)),
                navigation_ready=False,stage_open_verified=True,
                sha256=hashlib.sha256(output.read_bytes()).hexdigest())


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('scene');parser.add_argument('output')
    args=parser.parse_args()
    result=export_scene(json.loads(Path(args.scene).read_text(encoding='utf-8')),Path(args.output))
    print(json.dumps(result,ensure_ascii=False))
