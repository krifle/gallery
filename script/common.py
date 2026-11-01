"""Shared album validation and the GAL1 encryption contract."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess

LIMIT = 100_000_000
MAGIC = b"GAL1"
RESOURCE_RE = re.compile(r"media/[a-f0-9]{32}\.enc\Z")
ALBUM_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]*\Z")
IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEOS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
DEFAULTS = {
    "image_quality": 30, "image_size_mode": "longest", "image_size": 2000,
    "thumbnail_size": 480, "thumbnail_quality": 45, "frame_percent": 0,
    "video_bitrate_kbps": 1200, "video_size_mode": "longest", "video_size": 1280,
    "video_width": 1280, "video_height": 720, "video_max_fps": 30,
    "audio_bitrate_kbps": 96, "audio": True, "segment_seconds": 4,
    "av1_encoder": "libsvtav1", "av1_preset": 8, "poster_seconds": 1,
}


class AlbumError(Exception):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as exc:
        raise AlbumError(f"{path}: JSON {exc.lineno}행 {exc.colno}열 오류. 주석·쉼표를 확인하세요.") from None
    except (OSError, UnicodeError) as exc:
        raise AlbumError(f"JSON 파일을 읽을 수 없습니다: {path} ({type(exc).__name__})") from None


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AlbumError(f"중복 JSON 필드: {key}")
        result[key] = value
    return result


def write_json(path, value):
    atomic_write(Path(path), (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def album_path(value):
    album = Path(value).resolve()
    if not album.is_dir() or not ALBUM_RE.fullmatch(album.name):
        raise AlbumError("앨범 디렉터리가 없거나 이름이 올바르지 않습니다.")
    # Never follow private/output directory symlinks when writing a build.
    for name in (".prepare", ".prepare/media", ".prepare/manifest.json", ".prepare/config.json",
                 ".prepare/build.lock", ".prepare/converted", ".prepare/segments", ".prepare/cache", ".prepare/build", "media", "data"):
        path = album / name
        if path.is_symlink():
            raise AlbumError(f"앨범 디렉터리의 심볼릭 링크는 허용하지 않습니다: {name}")
    for name in ("converted", "segments", "cache", "build"):
        if any(path.is_symlink() for path in (album / ".prepare" / name).rglob("*")):
            raise AlbumError(f"작업 디렉터리의 심볼릭 링크는 허용하지 않습니다: {name}")
    return album


def safe_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise AlbumError("file은 비어 있지 않은 POSIX 상대경로여야 합니다.")
    raw = relative.split("/")
    if any(part in ("", ".", "..") for part in raw):
        raise AlbumError(f"안전하지 않은 상대경로: {relative}")
    path = PurePosixPath(relative)
    resolved = (Path(root) / relative).resolve()
    if path.is_absolute() or not resolved.is_relative_to(Path(root).resolve()):
        raise AlbumError(f"미디어 디렉터리를 벗어난 경로: {relative}")
    if not resolved.is_file():
        raise AlbumError(f"미디어 파일이 없습니다: {relative}")
    return resolved


def fields(obj, allowed, required, location):
    if not isinstance(obj, dict):
        raise AlbumError(f"{location}: 객체여야 합니다.")
    extra, missing = set(obj) - set(allowed), set(required) - set(obj)
    if extra or missing:
        raise AlbumError(f"{location}: 알 수 없는 필드 {sorted(extra)}, 필수 누락 {sorted(missing)}")
    for name in ("title", "description", "EXIF"):
        if name in obj and not isinstance(obj[name], str):
            raise AlbumError(f"{location}.{name}: 문자열이어야 합니다.")


def validate_source(album):
    data = read_json(album / ".prepare/manifest.json")
    fields(data, {"title", "days"}, {"days"}, "manifest")
    if not isinstance(data["days"], list) or not data["days"]:
        raise AlbumError("days는 비어 있지 않은 배열이어야 합니다.")
    names, real_paths, warnings = set(), set(), []
    root = album / ".prepare/media"
    for day in data["days"]:
        fields(day, {"date", "title", "description", "media"}, {"date", "media"}, "day")
        try:
            if not isinstance(day["date"], str) or date.fromisoformat(day["date"]).isoformat() != day["date"]:
                raise ValueError()
        except (TypeError, ValueError):
            raise AlbumError("day.date는 유효한 YYYY-MM-DD여야 합니다.") from None
        if not isinstance(day["media"], list):
            raise AlbumError("day.media는 배열이어야 합니다.")
        for item in day["media"]:
            fields(item, {"file", "title", "description", "EXIF"}, {"file"}, "media")
            path = safe_file(root, item["file"])
            if path.suffix.lower() not in IMAGES | VIDEOS:
                raise AlbumError(f"지원하지 않는 미디어: {item['file']}")
            if item["file"] in names or path in real_paths:
                raise AlbumError(f"중복 파일 참조: {item['file']}")
            names.add(item["file"])
            real_paths.add(path)
            if not path.name.startswith(day["date"].replace("-", "")):
                warnings.append(f"날짜와 파일명이 다릅니다 (그룹 유지): {day['date']} / {item['file']}")
        day["media"].sort(key=lambda item: (PurePosixPath(item["file"]).name, item["file"]))
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != ".DS_Store"}
    if actual - names:
        raise AlbumError(f"manifest에 등록되지 않은 파일: {', '.join(sorted(actual - names))}")
    data["days"].sort(key=lambda day: day["date"])
    return data, warnings


def validate_config(config):
    if not isinstance(config, dict) or set(config) != set(DEFAULTS):
        raise AlbumError("config 필드를 확인하세요. --write-default-config로 기본 설정을 생성할 수 있습니다.")
    for name, default in DEFAULTS.items():
        value = config[name]
        if isinstance(default, bool):
            if type(value) is not bool:
                raise AlbumError(f"{name}: true/false가 필요합니다.")
        elif isinstance(default, (int, float)):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise AlbumError(f"{name}: 유효한 숫자가 필요합니다.")
            low, high = (1, 10000)
            if "quality" in name: low, high = 0, 100
            if name == "frame_percent": low, high = 0, 30
            if name == "poster_seconds": low, high = 0, 3600
            if name == "av1_preset": low, high = (0, 13) if config["av1_encoder"] == "libsvtav1" else (0, 8)
            if name == "video_max_fps": low, high = 1, 60
            if name == "segment_seconds": low, high = 1, 10
            if not low <= value <= high:
                raise AlbumError(f"{name}: {low}~{high} 범위여야 합니다.")
            if name not in {"frame_percent", "poster_seconds", "segment_seconds", "video_max_fps"} and int(value) != value:
                raise AlbumError(f"{name}: 정수여야 합니다.")
            if name not in {"frame_percent", "poster_seconds", "segment_seconds", "video_max_fps"}:
                config[name] = int(value)
        elif not isinstance(value, str):
            raise AlbumError(f"{name}: 문자열이어야 합니다.")
    if config["image_size_mode"] not in {"original", "width", "longest"}:
        raise AlbumError("image_size_mode: original/width/longest 중 선택하세요.")
    if config["video_size_mode"] not in {"longest", "box"}:
        raise AlbumError("video_size_mode: longest/box 중 선택하세요.")
    if config["av1_encoder"] not in {"libsvtav1", "libaom-av1"}:
        raise AlbumError("지원 인코더: libsvtav1, libaom-av1")
    return config


def run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise AlbumError(f"필요한 도구를 찾을 수 없습니다: {command[0]}") from None
    if result.returncode:
        raise AlbumError(f"{command[0]} 실행 실패:\n{result.stderr[-3000:]}")
    return result.stdout


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def decode_key(text):
    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", text):
        raise AlbumError("키는 32바이트 난수를 표현한 43자 Base64URL이어야 합니다.")
    key = base64.urlsafe_b64decode(text + "=")
    if encode_key(key) != text:
        raise AlbumError("정규 Base64URL 키가 아닙니다.")
    return key


def encode_key(key):
    return base64.urlsafe_b64encode(key).decode().rstrip("=")


def aad(album_id, relative):
    if not ALBUM_RE.fullmatch(album_id) or not (relative == "data/manifest.enc" or RESOURCE_RE.fullmatch(relative)):
        raise AlbumError("암호화 리소스 경로가 잘못되었습니다.")
    return f"gallery:v1|{album_id}|{relative}".encode()


def encrypt_bytes(data, key, album_id, relative, nonces):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    while nonce in nonces:
        nonce = secrets.token_bytes(12)
    nonces.add(nonce)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, aad(album_id, relative))


def decrypt_bytes(data, key, album_id, relative):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(data) < 32 or data[:4] != MAGIC:
        raise AlbumError("GAL1 암호문 형식이 아닙니다.")
    try:
        return AESGCM(key).decrypt(data[4:16], data[16:], aad(album_id, relative))
    except InvalidTag:
        raise AlbumError("키가 올바르지 않거나 데이터가 손상되었습니다.") from None


def references(value):
    if isinstance(value, dict):
        if "path" in value and "bytes" in value:
            yield value
        else:
            for child in value.values():
                yield from references(child)
    elif isinstance(value, list):
        for child in value:
            yield from references(child)


@contextmanager
def build_lock(album):
    private = album / ".prepare"
    private.mkdir(exist_ok=True)
    # A stale file is harmless: the OS releases the advisory lock on exit.
    with (private / "build.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlbumError("이 앨범의 다른 변환/미리보기 작업이 진행 중입니다.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
