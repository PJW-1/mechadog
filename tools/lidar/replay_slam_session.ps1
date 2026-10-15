# 기록 세션 하나를 slam_toolbox 로 리플레이해 자체 일관 라이다 지도(PGM/YAML)를 받는다.
#   .\tools\lidar\replay_slam_session.ps1 -Session C:\dev\out\s0_patrol_1 -Out C:\dev\out\slam_maps\s0_patrol_1 [-Speed 3]
# 1) 컨테이너의 slam_toolbox 를 재시작해 지도를 비운다  2) replay_ros2.py 로 스캔·ODOM 송신
# 3) /map 한 장을 PGM/YAML 로 받아 Out 에 복사. 로봇은 움직이지 않는다.
param(
    [Parameter(Mandatory = $true)][string]$Session,
    [Parameter(Mandatory = $true)][string]$Out,
    [double]$Speed = 3.0,
    [string]$Container = "mechdog-ros2"
)
$ErrorActionPreference = "Continue"  # 네이티브 명령의 stderr 한 줄로 스크립트가 멈추지 않게
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
New-Item -ItemType Directory -Force -Path $Out | Out-Null

Write-Host "[1/4] 컨테이너 재시작 (브리지 seq 게이트 + slam_toolbox 지도 초기화)"
# scan_bridge 는 호스트와 같은 ScanDecoder seq 게이트를 쓴다 — 오늘 라이브로 더 큰 seq 를 이미 봤으면
# 같은 boot_id 의 옛 스캔(리플레이)을 전부 «역전» 으로 버린다. 게이트를 비우려면 재시작뿐이다.
docker restart $Container | Out-Null
Start-Sleep -Seconds 4
docker exec -d $Container bash -lc "source /opt/ros/jazzy/setup.bash; exec ros2 launch slam_toolbox online_async_launch.py slam_params_file:=/opt/mechdog/docker/ros2/slam.yaml > /tmp/slam_launch.log 2>&1"
Start-Sleep -Seconds 6
docker cp "$repo\docker\ros2\map_dump.py" "${Container}:/opt/mechdog/docker/ros2/map_dump.py" | Out-Null

Write-Host "[2/4] 리플레이: $Session (x$Speed)"
Push-Location $repo
try {
    python -X utf8 tools\lidar\replay_ros2.py $Session --speed $Speed 2>&1 | Tee-Object -FilePath "$Out\replay_ros2.log"
} finally { Pop-Location }
Start-Sleep -Seconds 5

Write-Host "[3/4] /map 저장"
docker exec $Container bash -lc "source /opt/ros/jazzy/setup.bash; timeout 120 python3 /opt/mechdog/docker/ros2/map_dump.py /tmp/out/slam_map" 2>&1 | Tee-Object -FilePath "$Out\map_dump.log"

Write-Host "[4/4] 복사 → $Out"
docker cp "${Container}:/tmp/out/slam_map.pgm" "$Out\slam_map.pgm" | Out-Null
docker cp "${Container}:/tmp/out/slam_map.yaml" "$Out\slam_map.yaml" | Out-Null
Get-ChildItem $Out | Select-Object Name, Length | Format-Table -AutoSize
