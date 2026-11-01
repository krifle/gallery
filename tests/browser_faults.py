"""Opt-in local sample tests for network failures, corruption, and private routes."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "script"))
from common import decode_key, decrypt_bytes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8770/2026-10/")
    parser.add_argument("--plain-base", default="http://127.0.0.1:8771/2026-10/")
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    key = args.key_file.read_text().strip()
    manifest = json.loads(decrypt_bytes((repo / "2026-10/data/manifest.enc").read_bytes(), decode_key(key), "2026-10", "data/manifest.enc"))
    image_ref = manifest["days"][0]["media"][0]["thumbnail"]["path"]
    video = manifest["days"][1]["media"][1]
    segment = next(t for t in video["tracks"] if t["kind"] == "video")["segments"][0]["path"]
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            for base in (args.base, args.plain_base):
                for path in (".prepare/manifest.json", ".prepare/config.json", ".prepare/media/20261003_074448.jpg", "../.git/config", "../.docs/2026-10-encrypted-gallery-plan.md", "../gallery-keys/2026-10.key"):
                    assert page.request.get(urljoin(base, path)).status == 404
                assert page.request.get(base, headers={"Host": "evil.example"}).status == 403
            assert page.request.get(urljoin(args.base, ".prepare/preview-manifest.json")).status == 404
            assert page.request.get(urljoin(args.plain_base, ".prepare/preview-manifest.json")).status == 200
            thumb_url = urljoin(args.base, image_ref)
            page.route(thumb_url, lambda route: route.fulfill(status=404))
            page.goto(args.base)
            page.locator("#key").fill(key)
            page.locator("#unlock-form button").click()
            page.locator(".thumbnail-retry").first.wait_for(state="visible")
            page.unroute(thumb_url)
            page.locator(".thumbnail-retry").first.click()
            page.wait_for_function("document.querySelectorAll('.tile.loaded').length===2")
            # Close while an image is still loading; immediately reopen another image.
            page.locator(".tile-open").first.click()
            page.locator("#viewer-close").click()
            page.locator(".tile-open").nth(1).click()
            page.locator(".viewer-image").wait_for()
            page.locator("#viewer-close").click()
            page.get_by_role("tab").nth(1).click()
            segment_url = urljoin(args.base, segment)
            page.route(segment_url, lambda route: route.fulfill(status=404))
            page.locator(".tile-open").nth(1).click()
            page.locator("#viewer-retry").wait_for(state="visible")
            assert "찾을 수 없습니다" in page.locator("#viewer-message").text_content()
            page.unroute(segment_url)
            page.locator("#viewer-retry").click()
            page.wait_for_function("document.querySelector('video')?.currentTime>0.5")
            page.locator("video").evaluate("async v=>{for(const t of [30,5,37,2]){v.currentTime=t;await new Promise(r=>setTimeout(r,50))}}")
            page.wait_for_function("document.querySelector('video')?.currentTime>3 && document.querySelector('video')?.currentTime<9")
            page.locator("#viewer-close").click()
            def corrupt(route):
                response = route.fetch()
                body = bytearray(response.body())
                body[-1] ^= 1
                route.fulfill(response=response, body=bytes(body))
            page.route(segment_url, corrupt)
            page.locator(".tile-open").nth(1).click()
            page.locator("#viewer-retry").wait_for(state="visible")
            assert "손상" in page.locator("#viewer-message").text_content()
            page.unroute(segment_url)
            page.locator("#viewer-close").click()
            page.locator(".tile-open").first.click()
            page.locator(".viewer-image").wait_for()
            assert not errors, errors
            print("PASS: private-route allowlist, Host check, thumbnail retry, segment 404/retry, rapid seek, GCM corruption, unaffected photos; no page errors")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
