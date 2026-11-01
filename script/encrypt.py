#!/usr/bin/env python3
"""Validate, convert and encrypt an album; never commits or pushes files."""
from __future__ import annotations

import argparse
import copy
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

from common import (AlbumError, DEFAULTS, LIMIT, MAGIC, RESOURCE_RE, album_path, atomic_write, build_lock,
                    decode_key, decrypt_bytes, encode_key, encrypt_bytes, read_json, references,
                    sha256, validate_config, validate_source, write_json)
from pipeline import convert_album, tools_check
from template import ensure_index

LABELS = {
    "image_quality": "사진 WebP 품질 (0~100)", "image_size_mode": "사진 크기 방식 (original/width/longest)",
    "image_size": "사진 너비/긴 변 (px)", "thumbnail_size": "썸네일 긴 변 (px)",
    "thumbnail_quality": "썸네일 품질 (0~100)", "frame_percent": "사진 흰 여백 (%)",
    "video_bitrate_kbps": "영상 bitrate (kbps)", "video_size_mode": "영상 크기 방식 (longest/box)",
    "video_size": "영상 긴 변 (px)", "video_width": "영상 bounding box 너비 (px)",
    "video_height": "영상 bounding box 높이 (px)", "video_max_fps": "영상 최대 fps",
    "audio_bitrate_kbps": "음성 bitrate (kbps)", "audio": "음성 보존 (y/n)",
    "segment_seconds": "세그먼트 길이 (초)", "av1_encoder": "AV1 인코더 (libsvtav1/libaom-av1)",
    "av1_preset": "AV1 preset", "poster_seconds": "포스터 추출 위치 (초)",
}


def load_config(album, path=None, interactive=True):
    path = Path(path) if path else album / ".prepare/config.json"
    config = read_json(path) if path.exists() else dict(DEFAULTS)
    if not interactive and not path.exists():
        raise AlbumError("비대화식 실행에는 config가 필요합니다. 먼저 --write-default-config를 실행하세요.")
    validate_config(config)
    if interactive:
        if not sys.stdin.isatty():
            raise AlbumError("입력 가능한 터미널에서 실행하거나 --non-interactive와 config를 사용하세요.")
        for name, value in config.items():
            answer = input(f"{LABELS[name]} [{value}]: ").strip()
            if not answer: continue
            if type(value) is bool:
                if answer.lower() not in {"y", "yes", "true", "n", "no", "false"}:
                    raise AlbumError("음성 보존 값은 y 또는 n입니다.")
                config[name] = answer.lower() in {"y", "yes", "true"}
            elif isinstance(value, (int, float)):
                try: config[name] = float(answer)
                except ValueError: raise AlbumError(f"{name}: 숫자를 입력하세요.") from None
            else: config[name] = answer
        validate_config(config)
    # Keep configuration out of arbitrary user-selected public directories.
    write_json(album / ".prepare/config.json", config)
    return config


def key_from_args(args, album):
    if args.new_key:
        key = secrets.token_bytes(32)
        if args.key_output:
            path = Path(args.key_output).expanduser().resolve()
            if path.is_relative_to(album.parent):
                raise AlbumError("새 키의 저장 위치는 gallery 저장소 밖이어야 합니다.")
            path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
                file.write(encode_key(key) + "\n")
            print(f"새 키를 저장했습니다 (덮어쓰기 없음, 권한 600): {path}", flush=True)
        else:
            if not sys.stdout.isatty():
                raise AlbumError("키를 로그에 출력하지 않습니다. --key-output으로 저장소 밖의 파일을 지정하세요.")
            print(f"새 키 (저장소 밖에 백업하세요): {encode_key(key)}", flush=True)
        return key
    if args.key_output:
        raise AlbumError("--key-output은 --new-key와 함께 사용하세요.")
    if args.key_file:
        return decode_key(Path(args.key_file).expanduser().read_text().strip())
    if args.key:
        print("안내: --key는 셸 기록에 남을 수 있습니다. --key-file 또는 숨김 입력을 권장합니다.", file=sys.stderr)
        return decode_key(args.key)
    if args.non_interactive:
        raise AlbumError("--key-file 또는 --key가 필요합니다.")
    return decode_key(getpass.getpass("복호화 키 (화면에 표시되지 않음): ").strip())


def encrypt_manifest(album, plain, key):
    """Stage a complete self-contained build and verify every encrypted resource."""
    build = album / ".prepare/build" / plain["buildId"]
    build.mkdir(parents=True, exist_ok=False)
    manifest = copy.deepcopy(plain)
    nonces, cache = set(), {}
    cache_path = album / ".prepare/cache/encryption.json"
    key_id = hashlib.sha256(key).hexdigest()
    if cache_path.exists():
        try: previous = read_json(cache_path)
        except AlbumError: previous = {}
        if isinstance(previous, dict) and previous.get("keyId") == key_id:
            cache = previous.get("resources", {})
    if not isinstance(cache, dict): cache = {}
    updated, by_plain = {}, {}
    for ref in references(manifest):
        source = album / ref["path"]
        if not source.resolve().is_relative_to(album / ".prepare"):
            raise AlbumError("평문 입력이 .prepare 밖에 있습니다.")
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest in by_plain:
            ref.update(by_plain[digest])
            continue
        previous = cache.get(digest, {})
        if not isinstance(previous, dict): previous = {}
        path = previous.get("path", "")
        encrypted = None
        if isinstance(path, str) and RESOURCE_RE.fullmatch(path) and (album / path).is_file():
            candidate = (album / path).read_bytes()
            try:
                if decrypt_bytes(candidate, key, album.name, path) == payload:
                    encrypted = candidate
                    nonces.add(candidate[4:16])
            except AlbumError:
                pass
        if encrypted is None:
            path = f"media/{secrets.token_hex(16)}.enc"
            encrypted = encrypt_bytes(payload, key, album.name, path, nonces)
        if decrypt_bytes(encrypted, key, album.name, path) != payload:
            raise AlbumError("암호화 왕복 검증에 실패했습니다.")
        atomic_write(build / path, encrypted)
        resource = {"path": path, "bytes": len(encrypted)}
        ref.update(resource)
        by_plain[digest] = resource
        updated[digest] = resource
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    encrypted = encrypt_bytes(payload, key, album.name, "data/manifest.enc", nonces)
    assert decrypt_bytes(encrypted, key, album.name, "data/manifest.enc") == payload
    atomic_write(build / "data/manifest.enc", encrypted)
    total = len(encrypted) + sum(ref["bytes"] for ref in by_plain.values())
    item_sizes = [{"id": item["id"], "bytes": sum(r["bytes"] for r in references(item))}
                  for day in manifest["days"] for item in day["media"]]
    write_json(build / "report.json", {"totalBytes": total, "limitBytes": LIMIT, "items": item_sizes})
    if total > LIMIT:
        print(json.dumps(item_sizes, indent=2))
        raise AlbumError(f"용량 초과: {total:,} / {LIMIT:,} bytes. bitrate·크기를 낮춘 뒤 다시 실행하세요. 공개 결과는 유지했습니다.")
    return build, manifest, {"keyId": key_id, "resources": updated}, total


def publish_build(album, build, manifest):
    wanted = {ref["path"] for ref in references(manifest)}
    existing = set()
    for folder in ("media", "data"):
        directory = album / folder
        if directory.exists():
            for file in directory.iterdir():
                relative = file.relative_to(album).as_posix()
                if file.is_symlink() or not file.is_file() or not (RESOURCE_RE.fullmatch(relative) or relative == "data/manifest.enc"):
                    raise AlbumError(f"공개 디렉터리에 예상하지 못한 파일이 있습니다: {relative}")
                if file.stat().st_size < 32 or file.read_bytes()[:4] != MAGIC:
                    raise AlbumError(f"공개 디렉터리에 암호문이 아닌 파일이 있습니다: {relative}")
                if folder == "media": existing.add(relative)
    new_files = []
    retired = {}
    old_manifest = (album / "data/manifest.enc").read_bytes() if (album / "data/manifest.enc").exists() else None
    manifest_replaced = False
    try:
        for relative in wanted:
            if relative not in existing:
                atomic_write(album / relative, (build / relative).read_bytes())
                new_files.append(relative)
            elif (album / relative).read_bytes() != (build / relative).read_bytes():
                raise AlbumError("기존 리소스의 불변 파일명이 충돌했습니다.")
        # The old manifest remains usable until all new files have been written.
        atomic_write(album / "data/manifest.enc", (build / "data/manifest.enc").read_bytes())
        manifest_replaced = True
        # Move retired ciphertext into the private build, so cleanup failures can roll back.
        for relative in existing - wanted:
            backup = build / "retired" / Path(relative).name
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(album / relative, backup)
            retired[relative] = backup
    except BaseException:
        for relative, backup in retired.items():
            os.replace(backup, album / relative)
        if manifest_replaced:
            if old_manifest is None: (album / "data/manifest.enc").unlink(missing_ok=True)
            else: atomic_write(album / "data/manifest.enc", old_manifest)
        for relative in new_files:
            (album / relative).unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album")
    parser.add_argument("--check", action="store_true", help="입력과 도구 검사만 수행")
    parser.add_argument("--write-default-config", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="암호화 없이 로컬 변환/미리보기 데이터 생성")
    parser.add_argument("--config")
    parser.add_argument("--non-interactive", action="store_true")
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument("--key")
    keys.add_argument("--key-file")
    keys.add_argument("--new-key", action="store_true")
    parser.add_argument("--key-output", help="새 키를 저장할 저장소 밖 경로")
    args = parser.parse_args(argv)
    try:
        album = album_path(args.album)
        with build_lock(album):
            if args.write_default_config:
                target = album / ".prepare/config.json"
                if target.exists(): raise AlbumError("기존 config는 덮어쓰지 않습니다.")
                write_json(target, DEFAULTS)
                print(f"기본 설정 생성: {target}")
                return 0
            source, warnings = validate_source(album)
            for warning in warnings: print(f"경고: {warning}")
            if not args.prepare_only:
                try:
                    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
                except ImportError:
                    raise AlbumError("cryptography가 필요합니다. 가상환경에서 pip install -r script/requirements.txt를 실행하세요.") from None
            if args.check:
                config_path = Path(args.config) if args.config else album / ".prepare/config.json"
                if args.config and not config_path.exists(): raise AlbumError("지정한 config 파일이 없습니다.")
                config = read_json(config_path) if config_path.exists() else dict(DEFAULTS)
                tools_check(validate_config(config))
                print("manifest·파일 대응·도구 검사 통과")
                return 0
            config = load_config(album, args.config, interactive=not args.non_interactive)
            ensure_index(album)
            key = None if args.prepare_only else key_from_args(args, album)
            plain = convert_album(album, source, config)
            if args.prepare_only:
                print("로컬 변환 완료. preview.py로 확인할 수 있습니다.")
                return 0
            build, manifest, cache, total = encrypt_manifest(album, plain, key)
            publish_build(album, build, manifest)
            write_json(album / ".prepare/cache/encryption.json", cache)
            write_json(album / ".prepare/build/current.json", {"buildId": manifest["buildId"], "totalBytes": total,
                        "files": {r["path"]: {"bytes": r["bytes"], "sha256": sha256(album / r["path"])} for r in references(manifest)},
                        "manifestSha256": sha256(album / "data/manifest.enc")})
            print(f"완료: {total:,} / {LIMIT:,} bytes. 공개 폴더에 암호문만 저장했습니다.")
            return 0
    except (AlbumError, OSError, ImportError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("작업을 중단했습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
