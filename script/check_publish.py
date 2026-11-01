#!/usr/bin/env python3
"""Check the Git index (default), working album, or a deployment artifact."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

from common import AlbumError, LIMIT, MAGIC, RESOURCE_RE, decode_key, decrypt_bytes, read_json, references


def private_path(path):
    parts = PurePosixPath(path).parts
    return any(part in {".prepare", ".docs", ".git", ".venv", "__pycache__"} for part in parts) or \
        any(part.startswith(".env") or part in {"preview.html", "preview-manifest.json", "album-key.txt"} or part.endswith((".key", ".pem")) for part in parts)


def check_files(files, read, album_id, key=None, report=None):
    """read() reads actual staged bytes in index mode, never working-tree substitutes."""
    errors = [f"비공개 파일 포함: {path}" for path in files if private_path(path)]
    total, media = 0, {}
    manifest_data = None
    for path in files:
        prefix = album_id + "/"
        if not path.startswith(prefix): continue
        relative = path[len(prefix):]
        if relative == "index.html": continue
        if not (relative == "data/manifest.enc" or RESOURCE_RE.fullmatch(relative)):
            errors.append(f"앨범 공개 경로에 허용하지 않은 파일: {path}")
            continue
        data = read(path)
        total += len(data)
        if len(data) < 32 or data[:4] != MAGIC:
            errors.append(f"잘못된 암호문 헤더/길이: {path}")
        if len(data) > 100 * 1024 * 1024:
            errors.append(f"단일 파일 한도 초과: {path}")
        if relative == "data/manifest.enc": manifest_data = data
        else: media[relative] = data
    if manifest_data is None: errors.append("암호화된 manifest가 없습니다.")
    if total > LIMIT: errors.append(f"100MB 초과: {total:,} bytes")
    if errors: raise AlbumError("\n".join(errors))
    expected = None
    if key:
        try: manifest = json.loads(decrypt_bytes(manifest_data, key, album_id, "data/manifest.enc"))
        except (ValueError, UnicodeError): raise AlbumError("manifest JSON이 손상되었습니다.") from None
        if manifest.get("schemaVersion") != 1 or manifest.get("albumId") != album_id:
            raise AlbumError("manifest 버전/앨범 ID가 다릅니다.")
        expected = {}
        for ref in references(manifest):
            path = ref["path"]
            if not RESOURCE_RE.fullmatch(path) or not isinstance(ref["bytes"], int):
                raise AlbumError("manifest 참조가 잘못되었습니다.")
            if path in expected and expected[path] != ref["bytes"]:
                raise AlbumError("manifest의 파일 크기가 서로 다릅니다.")
            expected[path] = ref["bytes"]
        for path, data in media.items(): decrypt_bytes(data, key, album_id, path)
    elif report:
        if report["manifestSha256"] != hashlib.sha256(manifest_data).hexdigest():
            raise AlbumError("로컬 빌드 보고서와 공개 manifest가 다릅니다. 다시 빌드하세요.")
        expected = {path: detail["bytes"] for path, detail in report["files"].items()}
        for path, data in media.items():
            if path not in report["files"] or hashlib.sha256(data).hexdigest() != report["files"][path]["sha256"]:
                raise AlbumError(f"빌드 보고서와 리소스가 다릅니다: {path}")
    if expected is not None:
        if set(expected) != set(media): raise AlbumError("manifest/빌드 목록과 공개 미디어 파일 목록이 다릅니다.")
        if any(len(media[path]) != size for path, size in expected.items()):
            raise AlbumError("미디어 크기가 manifest와 다릅니다.")
    return total, expected is not None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album", nargs="?", default="2026-10")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--working-tree", action="store_true")
    mode.add_argument("--artifact", type=Path)
    mode.add_argument("--all-staged", action="store_true", help="모든 staged 암호화 앨범과 비공개 경로 검사")
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args(argv)
    try:
        root = Path(__file__).resolve().parent.parent
        album_id = Path(args.album).name
        key = decode_key(args.key_file.expanduser().read_text().strip()) if args.key_file else None
        report = None
        if args.artifact:
            root = args.artifact.resolve()
            files = []
            for path in root.rglob("*"):
                if path.is_symlink(): raise AlbumError("배포 artifact에 심볼릭 링크가 있습니다.")
                if path.is_file(): files.append(path.relative_to(root).as_posix())
            read = lambda name: (root / name).read_bytes()
        elif args.working_tree:
            album = root / album_id
            paths = [p for p in album.rglob("*") if ".prepare" not in p.relative_to(album).parts and p.name != ".DS_Store"]
            if any(p.is_symlink() for p in paths): raise AlbumError("공개 폴더의 심볼릭 링크는 허용하지 않습니다.")
            files = [p.relative_to(root).as_posix() for p in paths if p.is_file()]
            read = lambda name: (root / name).read_bytes()
            report_path = album / ".prepare/build/current.json"
            if not key and report_path.exists(): report = read_json(report_path)
        else:
            output = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root).decode()
            entries = [entry.split("\t", 1) for entry in output.split("\0") if entry]
            if any(meta.split()[0] == "120000" and path.startswith(album_id + "/") for meta, path in entries):
                raise AlbumError("앨범의 staged 심볼릭 링크는 허용하지 않습니다.")
            if any(meta.split()[2] != "0" for meta, _ in entries): raise AlbumError("Git 충돌을 먼저 해결하세요.")
            blobs = {path: meta.split()[1] for meta, path in entries}
            files = list(blobs)
            read = lambda name: subprocess.check_output(["git", "cat-file", "blob", blobs[name]], cwd=root)
        if args.all_staged:
            forbidden = [path for path in files if private_path(path)]
            if forbidden: raise AlbumError("비공개 파일 포함: " + ", ".join(forbidden))
            albums = {path.split("/")[0] for path in files if path.endswith("/data/manifest.enc")}
            for path in files:
                if path.count("/") == 1 and path.endswith("/index.html") and b"../js/app.js" in read(path):
                    albums.add(path.split("/")[0])
            for name in sorted(albums):
                total, _ = check_files(files, read, name)
                print(f"{name}: staged 공개 경로/암호문 헤더/용량 검사 통과 ({total:,} bytes)")
            if not albums: print("비공개 파일 검사 통과. staged 암호화 앨범 없음.")
            return 0
        total, matched = check_files(files, read, album_id, key, report)
        verification = "참조 목록 일치" if matched else "헤더/경로만 검사 (키가 없어 인증·참조 검증 생략)"
        if key: verification += ", 전체 GCM 인증 통과"
        print(f"검사 통과: {total:,} / {LIMIT:,} bytes · {verification}")
        return 0
    except (AlbumError, OSError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
