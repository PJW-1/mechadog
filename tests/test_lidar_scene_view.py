"""The offline viewer must not expose arbitrary local files."""

from tools.lidar_scene_view import VENDOR, VIEWER, resolve_resource


def test_viewer_serves_scene_assets_and_only_map_photos(tmp_path):
    assert resolve_resource("/", tmp_path) == VIEWER / "index.html"
    assert resolve_resource("/scene.json", tmp_path) == tmp_path / "scene.json"
    assert resolve_resource("/viewer/main.js", tmp_path) == VIEWER / "main.js"
    assert resolve_resource("/vendor/three.module.js", tmp_path) == VENDOR / "three.module.js"
    assert (
        resolve_resource("/photos/step-0001.jpg", tmp_path) == tmp_path / "photos" / "step-0001.jpg"
    )


def test_viewer_blocks_traversal_and_unlisted_files(tmp_path):
    for path in (
        "/photos/../scene.json",
        "/photos/%2e%2e/scene.json",
        "/photos/%5c..%5csecret.jpg",
        "/viewer/../../config/config.yaml",
        "/vendor/../../server.py",
        "/config/config.yaml",
    ):
        assert resolve_resource(path, tmp_path) is None
