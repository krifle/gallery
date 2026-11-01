#!/usr/bin/env python3
"""Create an album skeleton without replacing existing files."""
from __future__ import annotations

import argparse
from datetime import date
from html import escape
import json
from pathlib import Path
import sys

from common import ALBUM_RE, AlbumError, DEFAULTS, album_path


def write_missing(path, text):
    if path.is_symlink():
        raise AlbumError(f"심볼릭 링크에는 파일을 생성하지 않습니다: {path}")
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(text)
        return True
    except FileExistsError:
        if not path.is_file():
            raise AlbumError(f"파일 위치에 디렉터리가 있습니다: {path}") from None
        return False


def ensure_index(album):
    path = album / "index.html"
    if path.is_file() and not path.is_symlink():
        return False
    template = (Path(__file__).parent / "templates/album.html").read_text(encoding="utf-8")
    return write_missing(path, template.replace("{{ALBUM_ID}}", escape(album.name)))


def create_album(album, event_date, title):
    if not ALBUM_RE.fullmatch(album.name):
        raise AlbumError("앨범 디렉터리 이름이 올바르지 않습니다.")
    album.mkdir(parents=True, exist_ok=True)
    album_path(album)
    for name in (".prepare/media", "data", "media"):
        (album / name).mkdir(parents=True, exist_ok=True)
    created = []
    if ensure_index(album): created.append("index.html")
    manifest = {"title": title, "days": [{"date": event_date, "title": title, "media": []}]}
    for name, data in (("manifest.json", manifest), ("config.json", DEFAULTS)):
        if write_missing(album / ".prepare" / name, json.dumps(data, ensure_ascii=False, indent=2) + "\n"):
            created.append(f".prepare/{name}")
    return created


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("album", help="새 앨범 경로 (예: 2026-11-01)")
    parser.add_argument("--date", help="첫 이벤트 날짜 YYYY-MM-DD; 생략하면 폴더명에서 추론")
    parser.add_argument("--title", default="여행 기록")
    args = parser.parse_args(argv)
    try:
        album = Path(args.album).resolve()
        if album.parent != Path(__file__).resolve().parent.parent:
            raise AlbumError("앨범은 gallery 저장소 바로 아래에 생성하세요.")
        value = args.date or (album.name + "-01" if len(album.name) == 7 else album.name)
        try:
            event_date = date.fromisoformat(value).isoformat()
            if event_date != value: raise ValueError()
        except ValueError:
            raise AlbumError("날짜를 폴더명에서 알 수 없습니다. --date YYYY-MM-DD를 지정하세요.") from None
        created = create_album(album, event_date, args.title)
        print(f"앨범 준비 완료: {album}")
        print("생성: " + (", ".join(created) if created else "없음 (기존 파일 유지)"))
        print(".prepare/media에 원본을 넣고 .prepare/manifest.json의 media에 파일을 등록하세요.")
        return 0
    except (AlbumError, OSError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
