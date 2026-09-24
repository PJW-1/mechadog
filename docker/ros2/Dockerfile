FROM ros:jazzy

RUN apt-get update && apt-get install -y --no-install-recommends \
    ros-jazzy-slam-toolbox ros-jazzy-rviz2 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/mechdog
ENV PYTHONPATH=/opt/mechdog
COPY host/__init__.py host/__init__.py
COPY host/common/__init__.py host/common/protocol.py host/common/units.py host/common/lidar_link.py host/common/
COPY docker/ros2/scan_bridge.py docker/ros2/scan_bridge.py
COPY docker/ros2/slam.yaml docker/ros2/slam.yaml

CMD ["python3", "docker/ros2/scan_bridge.py"]
