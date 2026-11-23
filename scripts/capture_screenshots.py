"""產生 README 用的介面截圖（docs/images/*.png）。

以示範模式啟動 Streamlit（暫存資料庫、不帶 API Key → 重播評測中的真實回答，不產生費用），
再用 Playwright 操作本機的 Microsoft Edge（channel="msedge"），不需另外下載瀏覽器。

用法：
  pip install -r requirements-dev.txt
  python -m scripts.capture_screenshots
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "images"
PORT = 8790
URL = f"http://127.0.0.1:{PORT}"


def wait_health(timeout: int = 60) -> None:
    for _ in range(timeout):
        try:
            if urllib.request.urlopen(f"{URL}/_stcore/health", timeout=2).read() == b"ok":
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError("Streamlit 未啟動")


def settle(page: Page, ms: int = 1500) -> None:
    """等 Streamlit 重新執行完畢（右上角執行中指示消失）。"""
    page.wait_for_timeout(500)
    try:
        page.wait_for_selector("[data-testid='stStatusWidget']", state="detached", timeout=60000)
    except Exception:  # noqa: BLE001 - 指示元件可能根本沒出現
        pass
    page.wait_for_timeout(ms)


def fit(page: Page) -> None:
    """Streamlit 主畫面在內部捲動：把視窗高度調成內容高度，才能一次截到完整內容。"""
    h = page.evaluate("() => { const m = document.querySelector('[data-testid=\"stMain\"]'); return m ? m.scrollHeight : 900; }")
    page.set_viewport_size({"width": 1440, "height": max(900, int(h) + 140)})
    settle(page, 800)


def login_as(page: Page, username: str) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(URL)
    page.wait_for_selector("text=以此角色登入", timeout=180000)
    settle(page)
    page.locator(f".st-key-demo_{username} button").click()
    settle(page)


def ask_sample(page: Page, index: int) -> None:
    btn = page.locator(f".st-key-sample_{index} button")
    if not btn.is_visible():
        page.get_by_text("範例問題（點選直接提問）").click()
        settle(page, 500)
    btn.click()
    page.wait_for_selector("text=示範重播", timeout=60000)
    settle(page)


def shot(page: Page, name: str, full: bool = True) -> None:
    path = OUT / name
    if full:
        fit(page)
    page.screenshot(path=str(path))
    print("saved", path.relative_to(ROOT))


def shot_exchange(page: Page, name: str) -> None:
    """只截最後一組「使用者問題＋系統回答」。"""
    fit(page)
    page.evaluate("() => { const m = document.querySelector('[data-testid=\"stMain\"]'); if (m) m.scrollTo(0, 0); window.scrollTo(0, 0); }")
    page.wait_for_timeout(500)
    msgs = page.locator("[data-testid='stChatMessage']")
    n = msgs.count()
    a, b = msgs.nth(n - 2).bounding_box(), msgs.nth(n - 1).bounding_box()
    bottom = b["y"] + b["height"] + 40
    vp = page.viewport_size
    if bottom > vp["height"]:  # 內容仍超出視窗 → 再放大視窗後重新量測
        page.set_viewport_size({"width": vp["width"], "height": int(bottom) + 200})
        page.wait_for_timeout(800)
        a, b = msgs.nth(n - 2).bounding_box(), msgs.nth(n - 1).bounding_box()
    pad = 16
    clip = {"x": max(0, a["x"] - pad), "y": max(0, a["y"] - pad), "width": max(a["width"], b["width"]) + 2 * pad,
            "height": (b["y"] + b["height"]) - a["y"] + 2 * pad}
    path = OUT / name
    page.screenshot(path=str(path), clip=clip)
    print("saved", path.relative_to(ROOT))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pqa_shots_"))
    env = {**os.environ, "PQA_DEMO_MODE": "1", "ANTHROPIC_API_KEY": "", "PQA_APP_DB": str(tmp / "app.db"),
           "PQA_CLEAN_DB": str(tmp / "clean.db"), "PQA_REPORT_DIR": str(tmp / "out"), "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.headless=true",
                             "--server.address=127.0.0.1", f"--server.port={PORT}", "--theme.base=light", "--client.toolbarMode=minimal"],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_health()
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

            page.goto(URL)
            page.wait_for_selector("text=以此角色登入", timeout=180000)   # 首次載入會先建立帳號與初始清理
            settle(page, 2000)
            shot(page, "01_login_demo.png")

            login_as(page, "r2_srv")
            shot(page, "02_sample_questions.png", full=False)
            ask_sample(page, 0)
            shot_exchange(page, "03_shortage_answer.png")
            ask_sample(page, 1)
            shot_exchange(page, "04_followup_po_alternates.png")

            login_as(page, "r1_scm")
            ask_sample(page, 0)
            shot_exchange(page, "05_bundle_substitution.png")

            login_as(page, "r2_edge")
            ask_sample(page, 0)
            shot_exchange(page, "06_permission_r2.png")

            login_as(page, "r4_thermal")
            ask_sample(page, 0)
            shot_exchange(page, "07_prompt_injection_chain.png")

            login_as(page, "r4_power")
            page.get_by_text("人工審核", exact=True).click()
            settle(page)
            shot(page, "08_r4_review.png", full=False)

            login_as(page, "r5_admin")
            page.locator("button:has-text('執行資料清理')").click()
            page.wait_for_selector("text=完成，耗時", timeout=120000)
            settle(page)
            shot(page, "09_r5_cleaning.png")
            page.get_by_text("稽核紀錄", exact=True).click()
            settle(page)
            shot(page, "10_r5_audit.png", full=False)
            browser.close()
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
