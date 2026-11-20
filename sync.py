import os
import io
import re
import hashlib
import socket
import ftplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

socket.setdefaulttimeout(30)

FTP_HOST = "ftp.meteo.gov.ua"
FTP_USER = os.environ["FTP_USER"]
FTP_PASS = os.environ["FTP_PASS"]
ROOT_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]

KYIV_TZ = timezone(timedelta(hours=2))

# Повні ключові слова (шукаються в будь-якій частині назви файлу)
SUBSTRING_KEYWORDS = (
    "aaxx",
    "aadd",
    "amdar",
    "avia",
    "base",
    "diagn",
    "eurdeperr",
    "ffbb",
    "grib",         # покриває і GRIB, і nogaps_GRIB2
    "hhss",
    "hhzz",
    "hmore",
    "hydrs",
    "metar",
    "prognoz",      # покриває і PROGNOZ, і PROGNOZ2
    "radiation",
    "sputn",        # покриває і Sputn, і Sputn_Evr_WatSteam
    "storm",
    "taf",
    "ttaa",
    "ttbb",
    "ttcc",
    "ttdd",
    "nogaps_grib2",
)

# Короткі префікси (шукаються на початку назви файлу або після розділювача, щоб не хапати зайве)
PREFIX_KEYWORDS = (
    "an",
    "pr",
    "ua",
    "u_",
    "w_",
)


def is_target_file(filename):
    name_low = filename.lower()
    if any(kw in name_low for kw in SUBSTRING_KEYWORDS):
        return True
    # Перевіряємо короткі префікси на початку імені або після '_' / '-' / '.'
    for pref in PREFIX_KEYWORDS:
        if name_low.startswith(pref) or re.search(rf"(^|[_\-\.]){re.escape(pref)}", name_low):
            return True
    return False


def connect_ftp():
    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.set_pasv(True)
    return ftp


def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds)


def get_or_create_folder(service, name, parent_id):
    query = (
        f"name = '{name}' and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute(num_retries=5)
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute(num_retries=5)
    print(f"Створено папку: {name}", flush=True)
    return folder["id"]


def get_existing_files(service, folder_id):
    """Отримує список файлів на Google Диску з їхнім реальним SHA-256 хешем"""
    existing = {}
    page_token = None
    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, sha256Checksum, appProperties)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute(num_retries=5)
        )
        for f in response.get("files", []):
            props = f.get("appProperties") or {}
            saved_hash = f.get("sha256Checksum") or props.get("content_sha256", "")
            existing[f["name"]] = {
                "id": f["id"],
                "sha256": saved_hash.lower(),
            }
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return existing


def collect_ftp_tasks(ftp, ftp_path, drive, drive_folder_id, tasks):
    """Обходить дерево папок FTP та збирає список цільових файлів для перевірки"""
    try:
        ftp.cwd(ftp_path)
    except ftplib.error_perm as e:
        print(f"Папка {ftp_path} ще не створена на FTP: {e}", flush=True)
        return

    existing_files = get_existing_files(drive, drive_folder_id)
    items = ftp.nlst()

    for item in items:
        if item in (".", ".."):
            continue
        full_ftp_path = f"{ftp_path}/{item}" if ftp_path != "/" else f"/{item}"
        try:
            ftp.cwd(full_ftp_path)
            sub_folder_id = get_or_create_folder(drive, item, drive_folder_id)
            collect_ftp_tasks(ftp, full_ftp_path, drive, sub_folder_id, tasks)
            ftp.cwd(ftp_path)
        except ftplib.error_perm:
            if is_target_file(item):
                tasks.append({
                    "ftp_path": ftp_path,
                    "full_ftp_path": full_ftp_path,
                    "name": item,
                    "drive_folder_id": drive_folder_id,
                    "existing_info": existing_files.get(item),
                })


def download_chunk(task_batch):
    """Завантажує групу файлів в окремому потоці через власне FTP-з'єднання"""
    results = []
    ftp = None
    try:
        ftp = connect_ftp()
        for task in task_batch:
            for attempt in range(1, 4):
                try:
                    ftp.cwd(task["ftp_path"])
                    bio = io.BytesIO()
                    ftp.retrbinary(f"RETR {task['name']}", bio.write)
                    file_bytes = bio.getvalue()
                    current_sha256 = hashlib.sha256(file_bytes).hexdigest().lower()
                    results.append((task, file_bytes, current_sha256))
                    break
                except Exception as e:
                    if attempt == 3:
                        print(f"Не вдалося завантажити {task['full_ftp_path']}: {e}", flush=True)
                    else:
                        try:
                            ftp.quit()
                        except Exception:
                            pass
                        time.sleep(1)
                        ftp = connect_ftp()
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass
    return results


def main():
    now = datetime.now(KYIV_TZ)
    if now.hour < 2:
        dates_to_check = [now - timedelta(days=1), now]
    else:
        dates_to_check = [now]

    print(f"Старт перевірки за {now.strftime('%Y-%m-%d %H:%M')}", flush=True)

    drive = get_drive_service()
    ftp = connect_ftp()
    tasks = []

    try:
        for target_date in dates_to_check:
            month_folder = target_date.strftime("%Y_%m")
            day_folder = target_date.strftime("%d_%m")
            month_id = get_or_create_folder(drive, month_folder, ROOT_FOLDER_ID)
            day_id = get_or_create_folder(drive, day_folder, month_id)
            collect_ftp_tasks(ftp, f"/{day_folder}", drive, day_id, tasks)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    print(f"Знайдено цільових файлів для звірки: {len(tasks)}", flush=True)

    num_workers = min(6, max(1, len(tasks)))
    batches = [tasks[i::num_workers] for i in range(num_workers)] if tasks else []

    downloaded_items = []
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(download_chunk, batch) for batch in batches]
        for future in as_completed(futures):
            downloaded_items.extend(future.result())

    total_changed = 0
    for task, file_bytes, current_sha256 in downloaded_items:
        existing_info = task["existing_info"]

        if existing_info and existing_info["sha256"] == current_sha256:
            continue

        bio = io.BytesIO(file_bytes)
        media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)
        app_props = {"content_sha256": current_sha256}

        for attempt in range(1, 6):
            try:
                if existing_info:
                    drive.files().update(
                        fileId=existing_info["id"],
                        body={"appProperties": app_props},
                        media_body=media,
                        fields="id",
                    ).execute(num_retries=5)
                    print(f"Оновлено (змінився вміст): {task['full_ftp_path']}", flush=True)
                else:
                    file_metadata = {
                        "name": task["name"],
                        "parents": [task["drive_folder_id"]],
                        "appProperties": app_props,
                    }
                    drive.files().create(
                        body=file_metadata, media_body=media, fields="id"
                    ).execute(num_retries=5)
                    print(f"Додано новий: {task['full_ftp_path']}", flush=True)

                total_changed += 1
                break
            except Exception as err:
                if attempt == 5:
                    print(f"Помилка запису {task['name']} на Диск: {err}", flush=True)
                else:
                    time.sleep(attempt * 3)

    print(f"Перевірку завершено. Додано або оновлено файлів: {total_changed}", flush=True)


if __name__ == "__main__":
    main()
