"""Open the same house USD and observe the same PC pose as the web, without physics."""
from pathlib import Path
import argparse
import hashlib
import json
import time
from urllib.request import urlopen, Request

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:8789/api/robot/state')
    parser.add_argument('--headless',action='store_true')
    parser.add_argument('--frames',type=int,default=0,help='0 for interactive; bounded capture otherwise')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--recorded-stop',help='Select an actual saved stop through the shared API')
    args=parser.parse_args()
    if args.headless and not 16<=args.frames<=600:
        parser.error('Headless capture requires --frames 16..600')
    if args.headless and not args.output:
        parser.error('Headless capture requires --output image.png')
    from robot_pose_client import RobotPoseClient
    client=RobotPoseClient(args.url)
    scene=ROOT/'exports/house_sim.usda'
    original_hash=hashlib.sha256(scene.read_bytes()).hexdigest()
    asset=json.loads((ROOT/'data/assets/robot/mechdog02_visual.json').read_text(encoding='utf8'))
    if args.recorded_stop:
        with urlopen(args.url,timeout=2) as response:
            status=json.load(response)
        request=Request(args.url.removesuffix('/state')+'/pose',data=json.dumps({
            'schema_version':1,'mode':'recorded','scene_revision':status['scene_revision'],
            'stop_id':args.recorded_stop}).encode(),headers={'Content-Type':'application/json'},method='POST')
        with urlopen(request,timeout=2) as response:
            json.load(response)
    from isaacsim import SimulationApp
    app=SimulationApp({'headless':args.headless,'multi_gpu':False,'max_gpu_count':1,
                       'renderer':'RaytracedLighting','limit_cpu_threads':8,'width':960,'height':600})
    report={'success':False,'hardware_commands':False,'physics_advanced':False,'poses':[],
            'requested_recorded_stop':args.recorded_stop}
    product=annotator=None
    try:
        import omni.usd
        import omni.replicator.core as rep
        import numpy as np
        from PIL import Image
        from pxr import Gf, UsdGeom
        from robot_overlay_usd import RobotOverlay
        context=omni.usd.get_context()
        if not context.open_stage(str(scene)):
            raise RuntimeError('House USD could not be opened')
        deadline=time.monotonic()+90
        while context.get_stage_loading_status()[2]>0:
            if time.monotonic()>deadline:
                raise TimeoutError('House stage loading timed out')
            app.update()
        stage=context.get_stage()
        revision=stage.GetRootLayer().customLayerData['sceneRevision']
        if UsdGeom.GetStageMetersPerUnit(stage)!=1 or str(UsdGeom.GetStageUpAxis(stage))!='Z':
            raise ValueError('House must use metres and Z-up')
        report['scene_revision']=revision
        stage.SetEditTarget(stage.GetSessionLayer())
        overlay=RobotOverlay(stage,asset)
        for prim in stage.GetPrimAtPath('/World/Objects').GetChildren():
            if prim.GetName().startswith('ceiling'):
                UsdGeom.Imageable(prim).MakeInvisible()
        client.start()
        # Initial reception deadline is bounded; interactive mode can remain visibly unregistered.
        deadline=time.monotonic()+4
        while client.current(revision) is None and time.monotonic()<deadline:
            app.update()
        pose=client.current(revision)
        if args.headless and pose is None:
            raise RuntimeError('No current displayable pose; refusing to invent a robot position')
        if args.headless and args.recorded_stop and pose.get('stop_id')!=args.recorded_stop:
            raise RuntimeError('Another viewer changed the selected stop before capture')
        if pose:
            x,y=pose['x_m'],pose['y_m']
            camera_path='/World/RobotObservationCamera'
            camera=UsdGeom.Camera.Define(stage,camera_path)
            from robot_observation_camera import inspection_camera
            scene_data=json.loads((ROOT/'data/scene.json').read_text(encoding='utf8'))
            eye,target,occluded=inspection_camera(scene_data,x,y)
            report['inspection_camera']={'position_m':eye,'target_m':target,'occluded_sample_count':occluded,'sample_count':4}
            camera.AddTransformOp().Set(Gf.Matrix4d().SetLookAt(
                Gf.Vec3d(*eye),Gf.Vec3d(*target),Gf.Vec3d(0,0,1)).GetInverse())
            camera.CreateFocalLengthAttr(20)
            camera.CreateVerticalApertureAttr(24)
            camera.CreateHorizontalApertureAttr(38.4)
            camera.CreateClippingRangeAttr(Gf.Vec2f(.02,100))
        else:
            camera_path='/World/Cameras/overview'
        if args.headless:
            rep.orchestrator.set_capture_on_play(False)
            product=rep.create.render_product(camera_path,(960,600))
            annotator=rep.AnnotatorRegistry.get_annotator('rgb')
            annotator.attach([product])
        else:
            from omni.kit.viewport.utility import get_active_viewport
            viewport=get_active_viewport()
            if viewport:
                viewport.camera_path=camera_path
        i=0
        last_signature=None
        while app.is_running() and (not args.frames or i<args.frames):
            pose=client.current(revision)
            if args.headless and args.recorded_stop and pose and pose.get('stop_id')!=args.recorded_stop:
                raise RuntimeError('Another viewer changed the selected stop during capture')
            overlay.update(pose)
            if pose:
                signature=(pose['x_m'],pose['y_m'],pose['yaw_rad'],pose['status'])
                if signature!=last_signature:
                    report['poses'].append(pose)
                    report['poses']=report['poses'][-100:]
                    last_signature=signature
            if args.headless:
                rep.orchestrator.step(rt_subframes=1,pause_timeline=True,delta_time=0.)
            else:
                app.update()
            i+=1
        if args.headless:
            if client.current(revision) is None:
                raise RuntimeError('Pose disappeared before capture finished')
            if args.recorded_stop and client.current(revision).get('stop_id')!=args.recorded_stop:
                raise RuntimeError('Selected stop changed before capture finished')
            pixels=annotator.get_data()
            if not isinstance(pixels,np.ndarray) or pixels.shape[:2]!=(600,960):
                raise RuntimeError('Missing or incorrect rendered image')
            args.output.parent.mkdir(parents=True,exist_ok=True)
            Image.fromarray(pixels[:,:,:3]).save(args.output)
            report['image_sha256']=hashlib.sha256(args.output.read_bytes()).hexdigest()
        report['frames']=i
        report['success']=True
    except Exception as exc:
        report['error']=f'{type(exc).__name__}: {exc}'
        raise
    finally:
        client.close()
        if annotator is not None:annotator.detach()
        if product is not None:product.destroy()
        report['source_usd_unchanged']=hashlib.sha256(scene.read_bytes()).hexdigest()==original_hash
        if args.output:
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.with_suffix('.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print(json.dumps(report,ensure_ascii=False),flush=True)
        app.close()


if __name__=='__main__':
    main()
