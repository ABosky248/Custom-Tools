# --- Install / setup external dependencies ---
import importlib.util
import subprocess
import sys
import time
import os
from pathlib import Path
from datetime import datetime
import re
import shutil
import threading

def ensure_package(import_name: str, pip_name: str | None = None) -> None:
    """Install package if not already installed."""
    if pip_name is None:
        pip_name = import_name
    if importlib.util.find_spec(import_name) is None:
        print(f"Installing {pip_name}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])

PACKAGES = [
    ("tqdm", None),
    ("dotenv", "python-dotenv"),
    ("playwright", None),
    ("requests", None),
    ("bs4", "beautifulsoup4"),
    ("keyboard", None),
]

for import_name, pip_name in PACKAGES:
    ensure_package(import_name, pip_name)

print("Checking Playwright browsers...")
subprocess.check_call([sys.executable, "-m", "playwright", "install"])
print("✓ Dependencies ready")

from tqdm import tqdm
from dotenv import load_dotenv, set_key
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from tkinter import Tk, filedialog, simpledialog
import keyboard

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

EXCLUDE_KEYWORDS = ["photos", "video", "dadisp", "iddas", "das", "diadem"]

USERNAME = None
PASSWORD = None
BASE_SAVE_DIR = None
DOWNLOAD_DIR = None
DATABASE_DIR = None

def ask_credentials():
    root = Tk()
    root.withdraw()

    email = simpledialog.askstring("Login", "Enter your email:")
    if email is None:
        root.destroy()
        return None, None

    password = simpledialog.askstring("Login", "Enter your password:", show="*")
    if password is None:
        root.destroy()
        return None, None

    save_choice = simpledialog.askstring("Save Credentials", "Save username/password to .env? (yes/no)")
    root.destroy()

    if save_choice and save_choice.strip().lower() in ("yes", "y"):
        env_path.touch(exist_ok=True)
        set_key(str(env_path), "USERNAME", email)
        set_key(str(env_path), "PASSWORD", password)
        print("Credentials saved to .env")

    return email, password

def make_base_folder():
    root = Tk()
    root.withdraw()
    selected = filedialog.askdirectory(title="Choose where to save downloaded data")
    root.destroy()
    if not selected:
        return None
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = Path(selected) / f"Saved Data {ts}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder

def initialize_paths():
    global USERNAME, PASSWORD, BASE_SAVE_DIR, DOWNLOAD_DIR, DATABASE_DIR

    load_dotenv(dotenv_path=env_path)
    USERNAME = os.getenv("USERNAME")
    PASSWORD = os.getenv("PASSWORD")

    if not USERNAME or not PASSWORD:
        USERNAME, PASSWORD = ask_credentials()
        if not USERNAME or not PASSWORD:
            raise ValueError("Missing username or password")

    BASE_SAVE_DIR = make_base_folder()
    if BASE_SAVE_DIR is None:
        raise ValueError("No save folder selected")

    DOWNLOAD_DIR = BASE_SAVE_DIR / "downloads"
    DATABASE_DIR = BASE_SAVE_DIR / "iihs_cache"
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_DIR.mkdir(parents=True, exist_ok=True)

def parse_test_title(raw_string: str):
    match = re.search(r"Test\s+([A-Z0-9]+):\s+(.*)", raw_string or "")
    if not match:
        return None, None
    return match.group(1), match.group(2).strip()

def save_download_from_click(page, click_target, save_dir: Path, timeout_ms: int = 120_000) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    with page.expect_download(timeout=timeout_ms) as download_info:
        click_target.click()
    download = download_info.value
    save_path = save_dir / download.suggested_filename
    download.save_as(str(save_path))
    print(f"Saved: {save_path}")
    return save_path

def copy_cached_test_if_exists(test_id: str, folder_name: str) -> bool:
    cached_test_dir = DATABASE_DIR / test_id
    if not cached_test_dir.exists():
        return False

    target_directory = DOWNLOAD_DIR / folder_name
    if target_directory.exists():
        return True

    print(f"Copying cached test {test_id} from database to working directory...")
    shutil.copytree(str(cached_test_dir), str(target_directory))
    return True

def mirror_test_to_cache(test_id: str, test_dir: Path):
    cache_target = DATABASE_DIR / test_id
    cache_target.mkdir(parents=True, exist_ok=True)

    for item in test_dir.iterdir():
        dest_path = cache_target / item.name
        if item.is_file():
            if not dest_path.exists():
                shutil.copy2(str(item), str(dest_path))
        elif item.is_dir():
            if not dest_path.exists():
                shutil.copytree(str(item), str(dest_path))
            else:
                for subitem in item.rglob("*"):
                    if subitem.is_file():
                        rel = subitem.relative_to(item)
                        dest_sub = dest_path / rel
                        dest_sub.parent.mkdir(parents=True, exist_ok=True)
                        if not dest_sub.exists():
                            shutil.copy2(str(subitem), str(dest_sub))

def get_folders(page):
    folder_list = page.locator(".folder-list")
    folder_list.wait_for(state="visible")

    anchors = folder_list.locator("a")
    count = anchors.count()
    folders = []

    for i in range(count):
        text = (anchors.nth(i).text_content() or "").strip()
        if not text:
            continue
        if any(kw in text.lower() for kw in EXCLUDE_KEYWORDS):
            continue
        folders.append(text)

    return folders

def is_excel_file(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith((".xls", ".xlsx", ".xlm", ".xlsm"))

def download_folder(page, folder_name: str, target_directory: Path):
    print(f"Downloading folder: {folder_name}")
    target_directory.mkdir(parents=True, exist_ok=True)

    folder_link = page.get_by_text(folder_name, exact=True)
    folder_link.click()
    page.wait_for_load_state("networkidle")

    file_cell = page.locator("td.file-list")
    file_cell.wait_for(state="visible")

    bulk_link = file_cell.locator("a").filter(has_text="Download all files in this folder")
    if bulk_link.count() > 0:
        try:
            save_download_from_click(page, bulk_link.first, target_directory)
        except PlaywrightTimeoutError:
            print(f"Timeout on 'Download all files' for {folder_name}, skipping.")
        except Exception as e:
            print(f"Error on 'Download all files' for {folder_name}: {e}")

    file_links = file_cell.locator("ul.AspNet-DataList td.filename a")
    count = file_links.count()
    print(f"Found {count} file links in '{folder_name}'")

    downloaded_count = 0
    for i in range(count):
        link = file_links.nth(i)
        text = (link.text_content() or "").strip()

        if not is_excel_file(text):
            print(f"Skipping non-Excel file link: {text}")
            continue

        print(f"Downloading Excel file link: {text}")

        try:
            save_download_from_click(page, link, target_directory)
            downloaded_count += 1
            time.sleep(3)
        except PlaywrightTimeoutError:
            print(f"Timeout downloading Excel link '{text}' in {folder_name}, skipping.")
        except Exception as e:
            print(f"Error downloading Excel link '{text}' in {folder_name}: {e}")

    if downloaded_count == 0:
        print(
            f"Warning: {target_directory} is empty after download. "
            f"Excel file links may not match filters or IIHS returned no data."
        )

def download_all_folders(page, target_directory: Path):
    folders = get_folders(page)
    for name in folders:
        if "excel" not in name.lower():
            continue
        download_folder(page, name, target_directory)

def login(page):
    page.goto("https://techdata.iihs.org/")
    page.wait_for_load_state("networkidle")
    page.get_by_text("Log in to IIHS TechData").click()
    page.get_by_placeholder("Email Address").fill(USERNAME)
    page.get_by_placeholder("Password").fill(PASSWORD)
    page.get_by_role("button", name="Sign In").click()
    page.wait_for_load_state("networkidle")
    page.get_by_text("Continue to downloads").click()
    page.wait_for_load_state("networkidle")

def select_dropdown(page, option_value=None, wait_time: int = 10):
    page.wait_for_load_state("networkidle")
    test_dropdown = page.locator("#ctl00_MainContentPlaceholder_TestTypeDropdown")
    test_dropdown.wait_for(state="visible")

    if option_value:
        test_dropdown.select_option(option_value)
    else:
        test_dropdown.click()
        print(f"⏳ Waiting {wait_time} seconds for manual selection. Search will begin after {wait_time} seconds")
        time.sleep(wait_time)

    search_button = page.locator("#ctl00_MainContentPlaceholder_SearchButton")
    search_button.wait_for(state="visible")
    search_button.click()
    page.wait_for_load_state("networkidle")

def return_to_downloads_list(page, page_value):
    try:
        if page.is_closed():
            return False
    except Exception:
        return False

    try:
        link = page.get_by_text("Return to downloads list", exact=True)
        link.wait_for(state="visible")
        link.click()
        page.wait_for_load_state("networkidle")

        page_selector = page.locator("#ctl00_MainContentPlaceholder_PageSelector_ddPages")
        page_selector.wait_for(state="visible")

        if page_value:
            page_selector.select_option(page_value)
            page.wait_for_load_state("networkidle")
            time.sleep(1)

        current_val = page_selector.evaluate("el => el.value")
        print(f"Returned to downloads list; current page selector value: {current_val}")
        return True
    except Exception as e:
        print(f"Could not return to downloads list: {e}")
        return False

def process_current_page(page, seen_test_ids: set, current_val) -> str:
    TEST_PREFIXES = [
        "SER", "LSCT", "CF", "CEF", "CES", "HSE", "CN", "CEP",
        "SWR", "AEB", "FCP", "VRU", "NVRU", "LAT", "SBR", "BSE", "LSC"
    ]

    page_selector = page.locator("#ctl00_MainContentPlaceholder_PageSelector_ddPages")
    page_selector.wait_for(state="visible")

    all_links = page.locator("a")
    count = all_links.count()

    test_link_indices = []
    for i in range(count):
        link_text = (all_links.nth(i).text_content() or "").strip()
        if any(prefix in link_text for prefix in TEST_PREFIXES):
            test_link_indices.append(i)

    print(f"Tests found on this page: {len(test_link_indices)}")

    if len(test_link_indices) == 0:
        return "done"

    visited_count = 0
    total_tests_on_page = len(test_link_indices)

    for i in tqdm(range(total_tests_on_page), desc="Tests on current page"):
        link_idx = test_link_indices[i]
        link = all_links.nth(link_idx)
        raw_link_text = (link.text_content() or "").strip()
        print(f"Processing link {i + 1}/{total_tests_on_page}: {raw_link_text}")

        try:
            with page.expect_navigation(timeout=30000):
                link.click()
        except PlaywrightTimeoutError:
            link.click()
            page.wait_for_load_state("load")

        h2_element = page.locator("h2").first
        raw_string = h2_element.text_content()
        print(f"Extracted text: {raw_string}")

        test_id, remaining_text = parse_test_title(raw_string)
        if not test_id:
            print("Could not parse expected 'Test ID' format; returning to downloads list.")
            if not return_to_downloads_list(page, current_val):
                return "done"
            visited_count += 1
            if visited_count >= total_tests_on_page:
                return "next_page"
            continue

        folder_name = f"{test_id}- {remaining_text}"
        test_dir = DOWNLOAD_DIR / folder_name
        test_dir.mkdir(parents=True, exist_ok=True)
        print(f"Created folder at: {test_dir.resolve()}")

        already_seen_in_run = test_id in seen_test_ids
        has_cached_copy = copy_cached_test_if_exists(test_id, folder_name)

        if not (already_seen_in_run or has_cached_copy):
            seen_test_ids.add(test_id)
            try:
                download_all_folders(page, test_dir)
            except Exception as e:
                print(f"Folder download failed for {folder_name}: {e}")
            mirror_test_to_cache(test_id, test_dir)
        else:
            print(f"Skipping download for duplicate test {test_id}.")

        if not return_to_downloads_list(page, current_val):
            return "done"

        visited_count += 1
        if visited_count >= total_tests_on_page:
            print("All links on this page have been visited; moving to next page.")
            return "next_page"

    return "continue"

def main():
    initialize_paths()

    if not USERNAME or not PASSWORD:
        raise ValueError("Missing USER or PASS")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            downloads_path=str(DOWNLOAD_DIR),
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        login(page)
        select_dropdown(page)

        page.wait_for_load_state("networkidle")
        print(f"Results URL captured: {page.url}")

        seen_test_ids = set()

        page_selector = page.locator("#ctl00_MainContentPlaceholder_PageSelector_ddPages")
        page_selector.wait_for(state="visible")
        options = page_selector.locator("option")
        num_pages = options.count()
        option_values = [options.nth(i).get_attribute("value") for i in range(num_pages)]

        current_val = page_selector.evaluate("el => el.value")
        try:
            current_idx = option_values.index(current_val)
        except ValueError:
            current_idx = 0
            current_val = option_values[0] if option_values else None

        while True:
            if current_val is None:
                print("No pages found. Done downloading tests")
                break

            try:
                page_selector.wait_for(state="visible")
                page_selector.select_option(current_val)
                page.wait_for_load_state("networkidle")
                time.sleep(1)
                print(f"Processing page value: {current_val}")
            except Exception as e:
                print(f"Failed to reselect current page {current_val}: {e}")
                break

            status = process_current_page(page, seen_test_ids, current_val)

            if status == "done":
                print("Done downloading tests")
                break

            try:
                page_selector = page.locator("#ctl00_MainContentPlaceholder_PageSelector_ddPages")
                page_selector.wait_for(state="visible")
                options = page_selector.locator("option")
                num_pages = options.count()
                option_values = [options.nth(i).get_attribute("value") for i in range(num_pages)]

                current_val = page_selector.evaluate("el => el.value")
                if current_val not in option_values:
                    current_val = option_values[0] if option_values else None

                current_idx = option_values.index(current_val)

                if current_idx >= num_pages - 1:
                    print("Already on last page. Done downloading tests")
                    break

                next_val = option_values[current_idx + 1]
                page_selector.select_option(next_val)
                page.wait_for_load_state("networkidle")
                time.sleep(2)

                current_val = next_val
                print(f"Moved to next page via dropdown; page selector value: {current_val}")
            except Exception as e:
                print(f"Failed to navigate to next page: {e}")
                break

        context.close()
        browser.close()

if __name__ == "__main__":
    main()
