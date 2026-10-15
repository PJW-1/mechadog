"""Use exactly the browser's canonical robot meshes as an observation overlay."""
import math
from pxr import Gf, Sdf, UsdGeom, UsdShade, Vt
from export_usd import linear


class RobotOverlay:
    def __init__(self, stage, asset):
        self.root=UsdGeom.Xform.Define(stage,'/World/ObservedMechaDog')
        self.translation=self.root.AddTranslateOp()
        self.rotation=self.root.AddRotateZOp()
        self.root.GetPrim().SetCustomDataByKey('hardwareCommands',False)
        self.root.GetPrim().SetCustomDataByKey('bodyAlignment',asset['base_alignment'])
        materials={}
        self.shaders=[]
        for name,spec in asset['materials'].items():
            path='/World/ObservedRobotMaterials/'+name
            material=UsdShade.Material.Define(stage,path)
            shader=UsdShade.Shader.Define(stage,path+'/Surface')
            shader.CreateIdAttr('UsdPreviewSurface')
            shader.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*[linear(v) for v in spec['color']]))
            shader.CreateInput('roughness',Sdf.ValueTypeNames.Float).Set(spec['roughness'])
            shader.CreateInput('metallic',Sdf.ValueTypeNames.Float).Set(spec['metalness'])
            material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(),'surface')
            materials[name]=material
            self.shaders.append((shader, list(spec['color'])))
        for i,part in enumerate(asset['parts']):
            mesh=UsdGeom.Mesh.Define(stage,f'/World/ObservedMechaDog/part_{i:03d}')
            mesh.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in part['vertices']]))
            mesh.CreateFaceVertexCountsAttr([3]*len(part['triangles']))
            mesh.CreateFaceVertexIndicesAttr([v for triangle in part['triangles'] for v in triangle])
            mesh.CreateNormalsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in part['normals']]))
            mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
            mesh.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(materials[part['material']])
        self.update(None)

    def update(self, pose):
        if pose is None:
            self.root.MakeInvisible()
            return False
        ghost=pose.get('status') in ('lost','stale')
        for shader, color in self.shaders:
            shader.GetInput('diffuseColor').Set(Gf.Vec3f(*([.32,.35,.38] if ghost else [linear(v) for v in color])))
            shader.CreateInput('opacity',Sdf.ValueTypeNames.Float).Set(.35 if ghost else 1.)
        self.translation.Set(Gf.Vec3d(pose['x_m'],pose['y_m'],0))
        self.rotation.Set(math.degrees(pose['yaw_rad']))
        self.root.GetPrim().SetCustomDataByKey('poseSource',pose['mode'])
        self.root.GetPrim().SetCustomDataByKey('poseStatus',pose['status'])
        self.root.GetPrim().SetCustomDataByKey('anchorFrame',pose.get('anchor_frame','unknown'))
        self.root.MakeVisible()
        return True
