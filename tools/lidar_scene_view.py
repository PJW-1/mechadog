"""Serve an exported scene locally for inspection; never connects to hardware."""

from __future__ import annotations

import argparse
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "tools" / "scene_viewer"
VENDOR = ROOT / "host" / "dashboard" / "static" / "vendor"


def resolve_resource(url_path: str, map_directory: Path) -> Path | None:
    """Resolve only the viewer, vendored JS, scene JSON, and map photos."""
    path = unquote(urlsplit(url_path).path)
    if path in ("/", "/viewer/index.html"):
        return VIEWER / "index.html"
    if path == "/scene.json":
        return map_directory / "scene.json"
    for prefix, base, suffixes in (
        ("/viewer/", VIEWER, {".js", ".css"}),
        ("/vendor/", VENDOR, {".js", ".txt"}),
        ("/photos/", map_directory / "photos", {".jpg", ".jpeg"}),
    ):
        if path.startswith(prefix):
            relative = path[len(prefix) :]
            if not relative or "\\" in relative:
                return None
            result = (base / relative).resolve()
            if result.is_relative_to(base.resolve()) and result.suffix.lower() in suffixes:
                return result
    return None


def serve(map_directory: Path, port: int) -> None:
    map_directory = map_directory.resolve()
    if not (map_directory / "scene.json").is_file():
        raise FileNotFoundError(f"scene.json이 없음: {map_directory}")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target = resolve_resource(self.path, map_directory)
            if target is None or not target.is_file():
                self.send_error(404)
                return
            content = target.read_bytes()
            mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"{mime}; charset=utf-8"
                if mime.startswith("text/")
                or mime in {"application/javascript", "application/json"}
                else mime,
            )
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)

    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        print(f"지도 보기: http://127.0.0.1:{server.server_port}/", flush=True)
        server.serve_forever()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map", type=Path, required=True, help="scene.json이 있는 폴더")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port는 0~65535")
    try:
        serve(args.map, args.port)
    except (OSError, ValueError) as exc:
        print(f"지도 뷰어 실패: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
