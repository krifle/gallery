"""Opt-in real Chrome test against running preview servers. Outputs stay in .prepare."""
import argparse
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8770/2026-10/")
    parser.add_argument("--plain-base", default="http://127.0.0.1:8771/2026-10/.prepare/preview.html")
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    key = args.key_file.read_text().strip()
    output = Path(__file__).resolve().parents[1] / ".prepare/qa"
    output.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1365, "height": 980})
        context.add_init_script("""window.liveBlobs = new Set();
          const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
          URL.createObjectURL = value => {const url=create(value); liveBlobs.add(url); return url};
          URL.revokeObjectURL = url => {liveBlobs.delete(url); revoke(url)};
        """)
        page = context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(args.base)
            assert page.locator("#unlock").is_visible()
            assert page.locator("#album").is_hidden()
            page.locator("#key").fill("A" * 43)
            page.locator("#unlock-form button").click()
            page.get_by_text("키가 올바르지 않거나 데이터가 손상되었습니다.", exact=True).wait_for()
            page.locator("#key").fill(key)
            page.locator("#unlock-form button").click()
            page.locator("#album").wait_for(state="visible")
            page.wait_for_function("document.querySelectorAll('.tile.loaded').length === 2")
            assert page.locator(".tile-caption").count() == 1
            page.screenshot(path=str(output / "gallery-desktop.png"), full_page=True, animations="disabled")
            page.locator(".tile-open").first.click()
            page.locator(".viewer-image").wait_for()
            assert page.locator(".viewer-image").evaluate("e=>e.naturalWidth>0")
            page.keyboard.press("ArrowRight")
            page.wait_for_function("document.querySelector('#viewer-count').textContent === '2 / 2'")
            page.keyboard.press("Escape")
            assert page.locator("#viewer").is_hidden()
            assert page.locator(".tile-open").first.evaluate("e=>e===document.activeElement")
            page.get_by_role("tab").nth(1).click()
            page.wait_for_function("document.querySelectorAll('.tile.loaded').length === 2")
            before = page.evaluate("liveBlobs.size")
            requests = []
            page.on("request", lambda r: requests.append(r.url))
            page.locator(".tile-open").nth(1).click()
            page.wait_for_function("document.querySelector('video')?.currentTime > 1", timeout=30000)
            video = page.locator("video")
            assert video.evaluate("v=>v.videoWidth>0 && v.videoHeight>0 && !v.error")
            initial_requests = len([r for r in requests if r.endswith(".enc")])
            # The sample is >40 seconds: 2 init + partial media must start playback.
            assert initial_requests < 24, initial_requests
            video.evaluate("v=>v.currentTime=30")
            page.wait_for_function("document.querySelector('video')?.currentTime > 31", timeout=30000)
            video.evaluate("v=>v.currentTime=3")
            page.wait_for_function("document.querySelector('video')?.currentTime > 4 && document.querySelector('video')?.currentTime < 10", timeout=30000)
            video.evaluate("v=>v.pause()")
            assert video.evaluate("v=>v.paused")
            video.evaluate("v=>v.play()")
            video.evaluate("v=>v.currentTime=v.duration-1")
            page.wait_for_function("document.querySelector('video')?.ended", timeout=20000)
            page.screenshot(path=str(output / "video-desktop.png"))
            page.locator("#viewer-close").click()
            page.wait_for_function("count=>liveBlobs.size===count", arg=before)
            for _ in range(3):
                page.locator(".tile-open").nth(1).click()
                page.wait_for_function("document.querySelector('video')?.currentTime > 0.1", timeout=30000)
                page.locator("#viewer-close").click()
                page.wait_for_function("count=>liveBlobs.size===count", arg=before)
            # Fragment key must disappear and not survive a reload in storage.
            page.goto(args.base)
            page.evaluate("key=>{location.hash='key='+key;location.reload()}", key)
            page.locator("#album").wait_for(state="visible")
            assert page.evaluate("location.hash") == ""
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0
            page.reload()
            assert page.locator("#unlock").is_visible()
            # Unavailable MSE should leave photos and tabs working.
            page.add_init_script("window.MediaSource = undefined")
            page.reload()
            page.locator("#key").fill(key)
            page.locator("#unlock-form button").click()
            page.locator("#album").wait_for(state="visible")
            page.get_by_role("tab").nth(1).click()
            page.locator(".tile-open").nth(1).click()
            page.get_by_text("이 브라우저에서는 이 AV1 영상을 재생할 수 없습니다.", exact=False).wait_for()
            page.locator("#viewer-close").click()
            page.locator(".tile-open").first.click()
            page.locator(".viewer-image").wait_for()
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
            mobile_page = mobile.new_page()
            mobile_page.on("pageerror", lambda e: errors.append(str(e)))
            mobile_page.goto(args.plain_base)
            mobile_page.locator("#album").wait_for(state="visible")
            mobile_page.wait_for_function("document.querySelectorAll('.tile.loaded').length === 2")
            assert mobile_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            mobile_page.screenshot(path=str(output / "gallery-mobile.png"), full_page=True, animations="disabled")
            mobile_page.get_by_role("tab").nth(1).click()
            mobile_page.locator(".tile-open").first.click()
            mobile_page.locator(".viewer-image").wait_for()
            mobile_page.screenshot(path=str(output / "image-mobile.png"))
            mobile.close()
            assert not errors, errors
            result = {"browser": browser.version, "desktop": "1365x980", "mobileEmulation": "390x844",
                      "initialVideoRequests": initial_requests, "checks": "unlock, wrong key, fragment, photos, tabs, AV1/AAC, seek both ways, end, reopen, Blob cleanup, unsupported, mobile layout", "pageErrors": errors}
            (output / "browser-results.json").write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2), flush=True)
        except Exception:
            page.screenshot(path=str(output / "failure.png"))
            print("Browser state:", page.locator("#viewer-message").text_content(), flush=True)
            print("Browser errors:", errors, flush=True)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    main()
