FROM ros:jazzy

RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-jazzy-slam-toolbox ros-jazzy-rviz2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mechdog
ENV PYTHONPATH=/opt/mechdog
COPY host/__init__.py host/__init__.py
COPY host/common/__init__.py host/common/protocol.py host/common/units.py host/common/lidar_link.py host/common/odom_link.py host/common/map_pose_link.py host/common/
COPY docker/ros2/scan_bridge.py docker/ros2/scan_bridge.py
COPY docker/ros2/odom_bridge.py docker/ros2/odom_bridge.py
COPY docker/ros2/pose_bridge.py docker/ros2/pose_bridge.py
COPY docker/ros2/slam.yaml docker/ros2/slam.yaml

# 세 브리지를 함께 띄운다. 하나라도 죽으면 컨테이너가 끝나 `docker ps` 에서 드러난다.
CMD ["bash", "-c", "python3 docker/ros2/odom_bridge.py & python3 docker/ros2/scan_bridge.py & python3 docker/ros2/pose_bridge.py & wait -n; exit $?"]
