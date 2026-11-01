"""Local-only media conversion. All plaintext output stays under .prepare."""
from __future__ import annotations

import copy
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import re
import secrets
import shutil
import xml.etree.ElementTree as ET

from common import AlbumError, IMAGES, probe, read_json, references, run, safe_file, sha256, write_json

NS = {"m": "urn:mpeg:dash:schema:mpd:2011"}
CACHE_VERSION = 1


def tools_check(config):
    version = run(["ffmpeg", "-version"]).splitlines()[0]
    run(["ffprobe", "-version"])
    encoders = run(["ffmpeg", "-hide_banner", "-encoders"])
    for encoder in ("libwebp", config["av1_encoder"], "aac"):
        if not re.search(rf"\b{encoder}\b", encoders):
            raise AlbumError(f"FFmpeg에 {encoder} 인코더가 없습니다. config에서 사용 가능한 인코더를 선택하세요.")
    return version


def display_dimensions(stream, frame=None):
    width, height = int(stream["width"]), int(stream["height"])
    # Scale to square pixels before deriving the final box.
    sar = stream.get("sample_aspect_ratio", "1:1")
    try:
        numerator, denominator = map(int, sar.split(":"))
        if numerator > 0 and denominator > 0:
            width = round(width * numerator / denominator)
    except (ValueError, ZeroDivisionError):
        pass
    rotations = [s.get("rotation", 0) for s in stream.get("side_data_list", [])]
    rotations += [stream.get("tags", {}).get("rotate", 0)]
    if frame:
        # JPEG EXIF rotation is exposed on the decoded frame, not the stream.
        rotations += [s.get("rotation", 0) for s in frame.get("side_data_list", [])]
        try:
            if int(frame.get("tags", {}).get("Orientation", "1")) in {5, 6, 7, 8}:
                rotations.append(90)
        except (ValueError, TypeError):
            pass
    if any(round(float(r)) % 180 == 90 for r in rotations):
        width, height = height, width
    return width, height


def dimensions(width, height, mode, size, box=None, even=False):
    if mode == "original": ratio = 1
    elif mode == "width": ratio = min(1, size / width)
    elif mode == "longest": ratio = min(1, size / max(width, height))
    else: ratio = min(1, box[0] / width, box[1] / height)
    unit = 2 if even else 1
    return tuple(max(unit, math.floor(n * ratio / unit) * unit) for n in (width, height))


def ffmpeg(arguments):
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *map(str, arguments)])


def sdr_filters(stream):
    if stream.get("color_transfer") not in {"smpte2084", "arib-std-b67"}:
        return []
    available = run(["ffmpeg", "-hide_banner", "-filters"])
    for name in ("zscale", "tonemap", "sidedata"):
        if not re.search(rf"\b{name}\b", available):
            raise AlbumError(f"HDR → SDR 변환에는 FFmpeg의 {name} 필터가 필요합니다. 해당 필터가 포함된 FFmpeg를 설치하세요.")
    # Linear floating-point RGB is required by tonemap. Only supply BT.2020
    # defaults when HDR input omits its primaries/matrix; preserve known tags.
    defaults = ""
    if stream.get("color_primaries") in {None, "unknown", "unspecified"}:
        defaults += ":pin=bt2020"
    if stream.get("color_space") in {None, "unknown", "unspecified"}:
        defaults += ":min=bt2020nc"
    return [f"zscale=t=linear:npl=100{defaults}", "format=gbrpf32le",
            "zscale=p=bt709", "tonemap=tonemap=hable:desat=2",
            "zscale=t=bt709:m=bt709:r=limited:dither=error_diffusion", "format=yuv420p",
            # Do not propagate HDR mastering/dynamic metadata to SDR output.
            "sidedata=mode=delete"]


def image_convert(source, output, width, height, quality, frame=0, seek=None, color_filters=()):
    output.parent.mkdir(parents=True, exist_ok=True)
    args = [] if seek is None else ["-ss", str(seek)]
    filters = [f"scale={width}:{height}", "setsar=1", *color_filters]
    if frame:
        filters.append(f"pad=iw+2*ceil(iw*{frame}/100):ih+2*ceil(ih*{frame}/100):ceil(iw*{frame}/100):ceil(ih*{frame}/100):color=white")
    ffmpeg([*args, "-i", source, "-map", "0:v:0", "-frames:v", "1", "-vf", ",".join(filters),
            "-c:v", "libwebp", "-preset", "photo", "-compression_level", "6", "-q:v", quality,
            "-map_metadata", "-1", "-map_chapters", "-1", output])


def local_ref(album, path):
    return {"path": path.relative_to(album).as_posix(), "bytes": path.stat().st_size}


def template_name(template, rep_id, number=None, time=None):
    value = template.replace("$RepresentationID$", str(rep_id))
    for token, number_value in (("Number", number), ("Time", time)):
        def substitute(match):
            if number_value is None:
                raise AlbumError("DASH 파일명 템플릿이 올바르지 않습니다.")
            return str(number_value).zfill(int(match[1] or 0))
        value = re.sub(rf"\${token}(?:%0(\d+)d)?\$", substitute, value)
    if "$" in value or Path(value).name != value:
        raise AlbumError("지원하지 않는 DASH 템플릿입니다.")
    return value


def parse_mpd(album, mpd):
    tree = ET.parse(mpd).getroot()
    tracks = []
    for adaptation in tree.findall(".//m:AdaptationSet", NS):
        for rep in adaptation.findall("m:Representation", NS):
            template = rep.find("m:SegmentTemplate", NS)
            if template is None:
                template = adaptation.find("m:SegmentTemplate", NS)
            if template is None:
                raise AlbumError("DASH SegmentTemplate이 없습니다.")
            mime = rep.get("mimeType", adaptation.get("mimeType", ""))
            kind = mime.split("/")[0]
            codecs = rep.get("codecs", "")
            if kind not in {"video", "audio"} or not codecs or not mime.endswith("/mp4"):
                raise AlbumError("지원하지 않는 DASH 트랙입니다.")
            scale = int(template.get("timescale", "1"))
            offset = int(template.get("presentationTimeOffset", "0"))
            index = int(template.get("startNumber", "1"))
            init = mpd.parent / template_name(template.attrib["initialization"], rep.attrib["id"])
            track = {"kind": kind, "mimeCodec": f'{mime}; codecs="{codecs}"',
                     "init": local_ref(album, init), "segments": []}
            timestamp = 0
            for entry in template.findall("m:SegmentTimeline/m:S", NS):
                timestamp = int(entry.get("t", timestamp))
                duration, repeat = int(entry.attrib["d"]), int(entry.get("r", "0"))
                if duration <= 0 or repeat < 0 or scale <= 0:
                    raise AlbumError("유효하지 않은 DASH 타임라인입니다.")
                for _ in range(repeat + 1):
                    name = template_name(template.attrib["media"], rep.attrib["id"], index, timestamp)
                    ref = local_ref(album, mpd.parent / name)
                    track["segments"].append({**ref, "index": len(track["segments"]),
                                              "start": (timestamp - offset) / scale, "duration": duration / scale})
                    index += 1
                    timestamp += duration
            if not track["segments"]:
                raise AlbumError("DASH 세그먼트가 없습니다.")
            # Verify container and random access on every segment, not just the MPD.
            scratch = mpd.parent / "verify.mp4"
            try:
                for segment in track["segments"]:
                    with scratch.open("wb") as output:
                        for file in (init, album / segment["path"]):
                            with file.open("rb") as source:
                                shutil.copyfileobj(source, output)
                    packets = json.loads(run(["ffprobe", "-v", "error", "-show_packets", "-show_entries",
                                              "packet=flags,pts_time,duration_time", "-of", "json", str(scratch)]))["packets"]
                    if not packets or (kind == "video" and "K" not in packets[0].get("flags", "")):
                        raise AlbumError("독립적으로 탐색할 수 없는 영상 세그먼트입니다.")
            finally:
                scratch.unlink(missing_ok=True)
            tracks.append(track)
    if sorted(t["kind"] for t in tracks) not in (["video"], ["audio", "video"]):
        raise AlbumError("영상 한 트랙과 선택적 음성 한 트랙이 필요합니다.")
    return tracks


def convert_item(album, item, config, version):
    source = safe_file(album / ".prepare/media", item["file"])
    metadata = probe(source)
    stream = next((s for s in metadata["streams"] if s["codec_type"] == "video"), None)
    if not stream:
        raise AlbumError(f"이미지/영상 스트림이 없습니다: {item['file']}")
    hdr = stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}
    width, height = display_dimensions(stream)
    if source.suffix.lower() in IMAGES:
        frames = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-read_intervals", "%+#1", "-show_frames", "-show_entries",
                                 "frame=width,height:frame_tags=Orientation:frame_side_data=rotation",
                                 "-of", "json", str(source)])).get("frames", [])
        if frames:
            width, height = display_dimensions(stream, frames[0])
    identity_parts = [CACHE_VERSION, item["file"], sha256(source), config, version]
    if hdr: identity_parts.append("hdr-sdr-hable-v1")
    if (width, height) != display_dimensions(stream):
        identity_parts.append("image-frame-orientation-v1")
    identity = json.dumps(identity_parts, sort_keys=True).encode()
    token = hashlib.sha256(identity).hexdigest()
    cache_path = album / ".prepare/cache" / f"{token}.json"
    if cache_path.exists():
        try:
            cached = read_json(cache_path)
            refs = list(references(cached["media"]))
            if refs and set(cached["hashes"]) == {r["path"] for r in refs} and all(safe_file(album, path).is_relative_to(album / ".prepare") and sha256(album / path) == digest
                   for path, digest in cached["hashes"].items()):
                return cached["media"]
        except (AlbumError, OSError, KeyError, TypeError):
            pass
    color_filters = sdr_filters(stream)
    if hdr:
        print(f"HDR → SDR 자동 변환: {item['file']}", flush=True)
    folder = album / ".prepare/converted" / token
    folder.mkdir(parents=True, exist_ok=True)
    thumb = folder / "thumbnail.webp"
    tw, th = dimensions(width, height, "longest", config["thumbnail_size"])
    result = {"id": secrets.token_hex(16)}
    if source.suffix.lower() in IMAGES:
        iw, ih = dimensions(width, height, config["image_size_mode"], config["image_size"])
        image_path = folder / "image.webp"
        image_convert(source, image_path, iw, ih, config["image_quality"], config["frame_percent"], color_filters=color_filters)
        image_convert(source, thumb, tw, th, config["thumbnail_quality"], color_filters=color_filters)
        actual = probe(image_path)["streams"][0]
        result.update(type="image", image=local_ref(album, image_path), mime="image/webp",
                      width=actual["width"], height=actual["height"])
    else:
        duration = float(metadata["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise AlbumError("영상 길이를 확인할 수 없습니다.")
        image_convert(source, thumb, tw, th, config["thumbnail_quality"], seek=min(config["poster_seconds"], duration / 2), color_filters=color_filters)
        vw, vh = dimensions(width, height, config["video_size_mode"], config["video_size"],
                            (config["video_width"], config["video_height"]), even=True)
        rate = stream.get("avg_frame_rate", "0/0")
        try: fps = min(float(Fraction(rate)), config["video_max_fps"])
        except (ValueError, ZeroDivisionError): fps = config["video_max_fps"]
        if fps <= 0: fps = config["video_max_fps"]
        segments = album / ".prepare/segments" / token
        segments.mkdir(parents=True, exist_ok=True)
        mpd = segments / "stream.mpd"
        sound = config["audio"] and any(s["codec_type"] == "audio" for s in metadata["streams"])
        encoder_options = (["-preset", config["av1_preset"], "-svtav1-params", "lp=2"] if config["av1_encoder"] == "libsvtav1"
                           else ["-cpu-used", config["av1_preset"], "-row-mt", "1", "-threads", "2"])
        args = ["-i", source, "-map", "0:v:0"]
        if sound: args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{int(config['audio_bitrate_kbps'])}k", "-ac", "2"]
        else: args += ["-an"]
        if hdr:
            args += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
        filters = [f"scale={vw}:{vh}", "setsar=1", f"fps={fps:.8f}", *color_filters]
        args += ["-vf", ",".join(filters), "-pix_fmt", "yuv420p",
                 "-c:v", config["av1_encoder"], *encoder_options, "-b:v", f"{int(config['video_bitrate_kbps'])}k",
                 "-g", math.ceil(fps * config["segment_seconds"]),
                 "-force_key_frames", f"expr:gte(t,n_forced*{config['segment_seconds']})",
                 "-map_metadata", "-1", "-map_chapters", "-1", "-f", "dash", "-dash_segment_type", "mp4",
                 "-seg_duration", config["segment_seconds"], "-use_template", "1", "-use_timeline", "1",
                 "-init_seg_name", "init-$RepresentationID$.mp4", "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
                 "-adaptation_sets", "id=0,streams=v id=1,streams=a" if sound else "id=0,streams=v", mpd]
        ffmpeg(args)
        tracks = parse_mpd(album, mpd)
        actual_duration = max(s["start"] + s["duration"] for t in tracks for s in t["segments"])
        result.update(type="video", width=vw, height=vh, duration=actual_duration, fps=fps, tracks=tracks)
    result["thumbnail"] = local_ref(album, thumb)
    write_json(cache_path, {"media": result, "hashes": {ref["path"]: sha256(album / ref["path"]) for ref in references(result)}})
    return result


def convert_album(album, source, config):
    version = tools_check(config)
    manifest = {"schemaVersion": 1, "albumId": album.name, "buildId": secrets.token_hex(16),
                "title": source.get("title", "여행 기록"), "days": []}
    count = sum(len(day["media"]) for day in source["days"])
    current = 0
    for day in source["days"]:
        target = {name: value for name, value in day.items() if name != "media"}
        target["media"] = []
        for item in day["media"]:
            current += 1
            print(f"[{current}/{count}] 변환/캐시 확인: {item['file']}", flush=True)
            converted = copy.deepcopy(convert_item(album, item, config, version))
            converted.update({name: item[name] for name in ("title", "description", "EXIF") if name in item})
            target["media"].append(converted)
        manifest["days"].append(target)
    write_json(album / ".prepare/preview-manifest.json", manifest)
    return manifest
