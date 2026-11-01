import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
from common import (AlbumError, DEFAULTS, decode_key, decrypt_bytes, encode_key, encrypt_bytes,
                    probe, read_json, references, validate_config, validate_source, write_json)
from encrypt import encrypt_manifest, publish_build
from check_publish import check_files
from pipeline import convert_album


class AlbumTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.album = Path(self.temporary.name).resolve() / "2026-10"
        self.private = self.album / ".prepare"
        (self.private / "media").mkdir(parents=True)
        self.key = secrets.token_bytes(32)

    def source(self, files=("20261003_b.jpg", "20261003_a.jpg")):
        for name in files: (self.private / "media" / name).write_bytes(b"fixture")
        manifest = {"days": [{"date": "2026-10-03", "media": [{"file": name} for name in files]}]}
        write_json(self.private / "manifest.json", manifest)
        return manifest

    def plain(self):
        path = self.private / "converted" / ("a" * 64) / "image.webp"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic plaintext image")
        ref = {"path": path.relative_to(self.album).as_posix(), "bytes": path.stat().st_size}
        return {"schemaVersion": 1, "albumId": self.album.name, "buildId": secrets.token_hex(16),
                "days": [{"date": "2026-10-03", "media": [{"id": "b" * 32, "type": "image", "image": ref}]}]}

    def test_source_sorted_and_optional_fields(self):
        self.source()
        manifest, warnings = validate_source(self.album)
        self.assertEqual([m["file"] for m in manifest["days"][0]["media"]], ["20261003_a.jpg", "20261003_b.jpg"])
        self.assertFalse(warnings)

    def test_comments_duplicates_unknown_and_types(self):
        file = self.private / "manifest.json"
        for text in ('{"days": [] // comment\n}', '{"days": [], "days": []}'):
            file.write_text(text)
            with self.assertRaises(AlbumError): validate_source(self.album)
        for update in ({"descirption": "typo"}, {"title": None}):
            data = self.source()
            data["days"][0].update(update)
            write_json(file, data)
            with self.assertRaises(AlbumError): validate_source(self.album)

    def test_missing_unlisted_duplicate_and_traversal(self):
        for name in ("missing.jpg", "../outside.jpg", "/tmp/outside.jpg", "20261003_a.jpg/../20261003_a.jpg"):
            data = self.source()
            data["days"][0]["media"][0]["file"] = name
            write_json(self.private / "manifest.json", data)
            with self.assertRaises(AlbumError): validate_source(self.album)
        data = self.source()
        data["days"][0]["media"][0] = data["days"][0]["media"][1]
        write_json(self.private / "manifest.json", data)
        with self.assertRaises(AlbumError): validate_source(self.album)
        data = self.source()
        (self.private / "media/unlisted.jpg").write_bytes(b"x")
        with self.assertRaises(AlbumError): validate_source(self.album)

    def test_symlink_escape(self):
        self.source(("20261003_a.jpg",))
        path = self.private / "media/20261003_a.jpg"
        path.unlink()
        outside = self.private / "outside.jpg"
        outside.write_bytes(b"private")
        path.symlink_to(outside)
        with self.assertRaises(AlbumError): validate_source(self.album)

    def test_config_bounds(self):
        self.assertEqual(validate_config(dict(DEFAULTS)), DEFAULTS)
        for name, value in (("image_quality", 101), ("audio", "yes"), ("image_size", 0), ("segment_seconds", float("nan")), ("video_size_mode", "stretch")):
            config = dict(DEFAULTS, **{name: value})
            with self.assertRaises(AlbumError): validate_config(config)

    def test_crypto_roundtrip_nonce_key_aad_and_tampering(self):
        path = "media/" + "a" * 32 + ".enc"
        nonces = set()
        a = encrypt_bytes(b"secret", self.key, "2026-10", path, nonces)
        b = encrypt_bytes(b"secret", self.key, "2026-10", path, nonces)
        self.assertNotEqual(a, b)
        self.assertEqual(decrypt_bytes(a, self.key, "2026-10", path), b"secret")
        for data, key, album, rel in ((a[:-1], self.key, "2026-10", path), (a[:-1] + bytes([a[-1] ^ 1]), self.key, "2026-10", path),
                                     (a, secrets.token_bytes(32), "2026-10", path), (a, self.key, "other", path),
                                     (a, self.key, "2026-10", "data/manifest.enc"), (b"bad", self.key, "2026-10", path)):
            with self.assertRaises(AlbumError): decrypt_bytes(data, key, album, rel)
        self.assertEqual(decode_key(encode_key(self.key)), self.key)
        for bad in ("password", "A" * 42, "A" * 44, "A" * 42 + "B"):
            with self.assertRaises(AlbumError): decode_key(bad)

    def test_ciphertext_cache_and_key_rotation(self):
        plain = self.plain()
        build, manifest, cache, _ = encrypt_manifest(self.album, plain, self.key)
        publish_build(self.album, build, manifest)
        write_json(self.private / "cache/encryption.json", cache)
        first = next(references(manifest))["path"]
        first_bytes = (self.album / first).read_bytes()
        plain["buildId"] = secrets.token_hex(16)
        plain["title"] = "changed metadata only"
        build, manifest, _, _ = encrypt_manifest(self.album, plain, self.key)
        self.assertEqual(next(references(manifest))["path"], first)
        self.assertEqual((build / first).read_bytes(), first_bytes)
        plain["buildId"] = secrets.token_hex(16)
        _, rotated, _, _ = encrypt_manifest(self.album, plain, secrets.token_bytes(32))
        self.assertNotEqual(next(references(rotated))["path"], first)

    def test_budget_failure_preserves_existing(self):
        plain = self.plain()
        build, manifest, _, _ = encrypt_manifest(self.album, plain, self.key)
        publish_build(self.album, build, manifest)
        original = (self.album / "data/manifest.enc").read_bytes()
        plain["buildId"] = secrets.token_hex(16)
        with patch("encrypt.LIMIT", 1), self.assertRaises(AlbumError):
            encrypt_manifest(self.album, plain, self.key)
        self.assertEqual((self.album / "data/manifest.enc").read_bytes(), original)

    def test_publish_failure_keeps_old_manifest_and_media(self):
        plain = self.plain()
        build, manifest, _, _ = encrypt_manifest(self.album, plain, self.key)
        publish_build(self.album, build, manifest)
        original = {p.relative_to(self.album).as_posix(): p.read_bytes() for folder in ("media", "data") for p in (self.album / folder).iterdir()}
        plain["buildId"] = secrets.token_hex(16)
        build, manifest, _, _ = encrypt_manifest(self.album, plain, self.key)
        from common import atomic_write
        def fail_manifest(path, data):
            if path == self.album / "data/manifest.enc": raise OSError("simulated disk error")
            return atomic_write(path, data)
        with patch("encrypt.atomic_write", fail_manifest), self.assertRaises(OSError): publish_build(self.album, build, manifest)
        after = {p.relative_to(self.album).as_posix(): p.read_bytes() for folder in ("media", "data") for p in (self.album / folder).iterdir()}
        self.assertEqual(original, after)

    def test_publish_checker_rejects_private_plain_extra_and_missing(self):
        plain = self.plain()
        build, manifest, _, total = encrypt_manifest(self.album, plain, self.key)
        files = {f"2026-10/{r['path']}": (build / r["path"]).read_bytes() for r in references(manifest)}
        files["2026-10/data/manifest.enc"] = (build / "data/manifest.enc").read_bytes()
        self.assertEqual(check_files(files, files.__getitem__, "2026-10", self.key)[0], total)
        for bad_name in (".docs/plan.md", "2026-10/.prepare/manifest.json", "2026-10/media/photo.jpg", "secret.key"):
            bad = dict(files, **{bad_name: b"oops"})
            with self.assertRaises(AlbumError): check_files(bad, bad.__getitem__, "2026-10", self.key)
        missing = {"2026-10/data/manifest.enc": files["2026-10/data/manifest.enc"]}
        with self.assertRaises(AlbumError): check_files(missing, missing.__getitem__, "2026-10", self.key)

    def test_cleanup_failure_rolls_back_published_manifest(self):
        plain = self.plain()
        build, manifest, _, _ = encrypt_manifest(self.album, plain, self.key)
        publish_build(self.album, build, manifest)
        original = {p.relative_to(self.album).as_posix(): p.read_bytes() for folder in ("media", "data") for p in (self.album / folder).iterdir()}
        plain["buildId"] = secrets.token_hex(16)
        build, manifest, _, _ = encrypt_manifest(self.album, plain, self.key)
        real_replace = os.replace
        def fail_retirement(source, target):
            if Path(target).parent.name == "retired": raise OSError("simulated retirement error")
            return real_replace(source, target)
        with patch("encrypt.os.replace", fail_retirement), self.assertRaises(OSError):
            publish_build(self.album, build, manifest)
        after = {p.relative_to(self.album).as_posix(): p.read_bytes() for folder in ("media", "data") for p in (self.album / folder).iterdir()}
        self.assertEqual(original, after)

    def test_node_webcrypto_interoperability(self):
        if not shutil.which("node"): self.skipTest("Node is needed for Web Crypto interop")
        path = "media/" + "a" * 32 + ".enc"
        cipher = encrypt_bytes(b"cross-language fixture", self.key, "2026-10", path, set())
        module = (Path(__file__).resolve().parents[1] / "js/crypto.js").as_uri()
        code = '''import {importKey, decrypt} from MODULE;
import {readFileSync} from 'node:fs';
const input = JSON.parse(readFileSync(0,'utf8'));
const key = await importKey(input.key);
const bytes = Uint8Array.from(input.bytes);
const output = await decrypt(bytes, key, '2026-10', input.path);
if (new TextDecoder().decode(output) !== 'cross-language fixture') process.exit(1);
bytes[bytes.length-1] ^= 1;
try {await decrypt(bytes,key,'2026-10',input.path); process.exit(2)} catch {}
'''.replace("MODULE", json.dumps(module))
        result = subprocess.run(["node", "--input-type=module", "-e", code], input=json.dumps({"key": encode_key(self.key), "bytes": list(cipher), "path": path}), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_index_checker_reads_staged_bytes_not_working_copy(self):
        repo = Path(self.temporary.name).resolve() / "repo"
        (repo / "script").mkdir(parents=True)
        scripts = Path(__file__).resolve().parents[1] / "script"
        for name in ("common.py", "check_publish.py"):
            shutil.copy2(scripts / name, repo / "script" / name)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        data = repo / "2026-10/data/manifest.enc"
        data.parent.mkdir(parents=True)
        data.write_bytes(b"plaintext accidentally staged")
        subprocess.run(["git", "add", "2026-10"], cwd=repo, check=True)
        data.write_bytes(b"GAL1" + b"x" * 50)
        result = subprocess.run([sys.executable, "script/check_publish.py", "--all-staged"], cwd=repo, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("헤더", result.stderr)
        subprocess.run(["git", "add", "2026-10"], cwd=repo, check=True)
        result = subprocess.run([sys.executable, "script/check_publish.py", "--all-staged"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        secret = repo / "2026-10/.prepare/manifest.json"
        secret.parent.mkdir()
        secret.write_text('{"private":true}')
        subprocess.run(["git", "add", "2026-10/.prepare"], cwd=repo, check=True)
        secret.unlink()
        result = subprocess.run([sys.executable, "script/check_publish.py", "--all-staged"], cwd=repo, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("비공개", result.stderr)


@unittest.skipUnless(os.environ.get("GALLERY_MEDIA_TESTS") == "1", "Set GALLERY_MEDIA_TESTS=1 to encode synthetic video")
class MediaTests(unittest.TestCase):
    setUp = AlbumTests.setUp
    def test_jpeg_exif_rotation_preserves_aspect_ratio(self):
        original = self.private / "original.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=160x120", "-frames:v", "1", str(original)], check=True)
        for orientation in (1, 3, 5, 6, 7, 8):
            with self.subTest(orientation=orientation):
                # A minimal EXIF IFD containing just the orientation tag.
                exif = (b"Exif\0\0II" + struct.pack("<HIH", 42, 8, 1) +
                        struct.pack("<HHI", 274, 3, 1) + struct.pack("<H", orientation) +
                        b"\0\0" + struct.pack("<I", 0))
                jpeg = original.read_bytes()
                source = self.private / "media" / f"20261003_rotation_{orientation}.jpeg"
                source.write_bytes(jpeg[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + jpeg[2:])
                manifest = {"days": [{"date": "2026-10-03", "media": [{"file": source.name}]}]}
                config = dict(DEFAULTS, image_size=120, thumbnail_size=64)
                result = convert_album(self.album, manifest, config)
                item = result["days"][0]["media"][0]
                rotated = orientation in (5, 6, 7, 8)
                self.assertEqual((item["width"], item["height"]), (90, 120) if rotated else (120, 90))
                thumbnail = probe(self.album / item["thumbnail"]["path"])["streams"][0]
                self.assertEqual((thumbnail["width"], thumbnail["height"]), (48, 64) if rotated else (64, 48))

    # Keep the expensive test separate from fast regression tests.
    def test_short_silent_video(self):
        source = self.private / "media/20261003_silent.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=128x96:rate=12", "-t", "0.8", "-an", "-c:v", "libx264", str(source)], check=True)
        write_json(self.private / "manifest.json", {"days": [{"date": "2026-10-03", "media": [{"file": source.name}]}]})
        manifest, _ = validate_source(self.album)
        result = convert_album(self.album, manifest, dict(DEFAULTS))
        item = result["days"][0]["media"][0]
        self.assertEqual([track["kind"] for track in item["tracks"]], ["video"])
        self.assertEqual(len(item["tracks"][0]["segments"]), 1)
        self.assertLess(item["duration"], 1)

    def test_hdr_hlg_and_pq_convert_to_bt709(self):
        for transfer in ("arib-std-b67", "smpte2084"):
            with self.subTest(transfer=transfer):
                source = self.private / "media" / f"20261003_{transfer}.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                                "testsrc2=size=128x96:rate=12", "-t", "0.5", "-an", "-c:v", "libx264",
                                "-vf", f"setparams=color_primaries=bt2020:color_trc={transfer}:colorspace=bt2020nc",
                                "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
                                "-colorspace", "bt2020nc", "-color_trc", transfer, str(source)], check=True)
                self.assertEqual(probe(source)["streams"][0]["color_transfer"], transfer)
                manifest = {"days": [{"date": "2026-10-03", "media": [{"file": source.name}]}]}
                result = convert_album(self.album, manifest, dict(DEFAULTS))
                item = result["days"][0]["media"][0]
                track = item["tracks"][0]
                combined = self.private / "verify-sdr.mp4"
                combined.write_bytes((self.album / track["init"]["path"]).read_bytes() +
                                     (self.album / track["segments"][0]["path"]).read_bytes())
                stream = probe(combined)["streams"][0]
                self.assertEqual([stream[k] for k in ("color_primaries", "color_transfer", "color_space")],
                                 ["bt709"] * 3)
                self.assertEqual(stream["pix_fmt"], "yuv420p")
                thumbnail = probe(self.album / item["thumbnail"]["path"])["streams"][0]
                self.assertEqual((thumbnail["width"], thumbnail["height"]), (128, 96))
                cached = convert_album(self.album, manifest, dict(DEFAULTS))
                self.assertEqual(cached["days"][0]["media"][0], item)


if __name__ == "__main__":
    unittest.main()
