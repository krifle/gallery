#!/usr/bin/env python3
"""Serve an explicit allowlist on localhost; no directory listing or repository-wide access."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

from common import AlbumError, RESOURCE_RE, album_path, atomic_write, build_lock, references, safe_file, validate_source
from encrypt import load_config
from pipeline import convert_album
from template import ensure_index

APP_FILES = ("css/encrypted-gallery.css", "js/app.js", "js/crypto.js", "js/gallery.js", "js/player.js", "js/manifest.js")


def generate_preview(album, manifest):
    private = album / ".prepare"
    ensure_index(album)
    template = (album / "index.html").read_text()
    template = template.replace('href="../css/', 'href="../../css/').replace('src="../js/app.js"', 'src="./preview-app.js"')
    template = template.replace('href="../"', 'href="../index.html"')
    atomic_write(private / "preview.html", template.encode())
    entry = '''import { createGallery } from '../../js/gallery.js';
import { validateManifest } from '../../js/manifest.js';
import { fetchBuffer } from '../../js/crypto.js';
const albumId = ALBUM_ID;
const base = new URL('../', location.href);
try {
  const data = JSON.parse(new TextDecoder().decode(await fetchBuffer(new URL('preview-manifest.json', location.href), {cache:'no-store'})));
  validateManifest(data, albumId, {plain:true});
  document.querySelector('#unlock').hidden = true;
  createGallery(data, (ref, signal) => fetchBuffer(new URL(ref.path, base), {signal, bytes:ref.bytes, cache:'no-store'}));
} catch (error) { document.querySelector('#unlock-message').textContent = error.message; }
'''.replace('ALBUM_ID', json.dumps(album.name))
    atomic_write(private / "preview-app.js", entry.encode())


def routes_for(album, plain_manifest=None):
    repo = album.parent
    paths = {"/" + name: repo / name for name in APP_FILES}
    paths["/"] = repo / "index.html"
    paths["/styles.css"] = repo / "styles.css"
    paths[f"/{album.name}/"] = album / "index.html"
    paths[f"/{album.name}/index.html"] = album / "index.html"
    manifest_path = album / "data/manifest.enc"
    if manifest_path.is_file(): paths[f"/{album.name}/data/manifest.enc"] = manifest_path
    for path in (album / "media").glob("*.enc"):
        if RESOURCE_RE.fullmatch(path.relative_to(album).as_posix()):
            paths[f"/{album.name}/media/{path.name}"] = path
    if plain_manifest:
        for name in ("preview.html", "preview-app.js", "preview-manifest.json"):
            paths[f"/{album.name}/.prepare/{name}"] = album / ".prepare" / name
        for ref in references(plain_manifest):
            path = safe_file(album, ref["path"])
            if not path.is_relative_to(album / ".prepare"):
                raise AlbumError("잘못된 미리보기 경로입니다.")
            paths[f"/{album.name}/{ref['path']}"] = path
    return paths


class PreviewHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, routes, **kwargs):
        self.routes = routes
        super().__init__(*args, **kwargs)

    def log_message(self, *_):
        # Do not log URL query strings, potentially containing mistakenly pasted keys.
        pass

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve()

    def serve(self, head=False):
        host = self.headers.get("Host", "").split(":")[0]
        if host not in {"127.0.0.1", "localhost"}:
            self.send_error(403)
            return
        route = unquote(urlsplit(self.path).path)
        path = self.routes.get(route)
        if path is None or not path.is_file() or path.is_symlink():
            self.send_error(404)
            return
        content = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js": mime = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        if not head:
            try: self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError): pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album")
    parser.add_argument("--encrypted", action="store_true")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--config")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args(argv)
    try:
        album = album_path(args.album)
        plain = None
        if not args.encrypted:
            with build_lock(album):
                source, warnings = validate_source(album)
                for warning in warnings: print(f"경고: {warning}")
                # Existing saved settings make normal preview runs prompt-free.
                interactive = not args.non_interactive and not (album / ".prepare/config.json").exists()
                config = load_config(album, args.config, interactive=interactive)
                plain = convert_album(album, source, config)
                generate_preview(album, plain)
        elif not (album / "data/manifest.enc").is_file():
            raise AlbumError("먼저 encrypt.py로 암호문을 생성하세요.")
        else:
            with build_lock(album):
                ensure_index(album)
        server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(PreviewHandler, routes=routes_for(album, plain)))
        suffix = ".prepare/preview.html" if plain else ""
        print(f"미리보기: http://127.0.0.1:{server.server_port}/{album.name}/{suffix}", flush=True)
        print("이 컴퓨터에서만 접근할 수 있습니다. 종료: Ctrl+C", flush=True)
        try: server.serve_forever()
        finally: server.server_close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (AlbumError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
