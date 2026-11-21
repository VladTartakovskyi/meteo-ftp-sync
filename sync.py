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

SUBSTRING_KEYWORDS = (
    "aaxx",
    "aadd",
    "amdar",
    "avia",
    "base",
    "diagn",
    "eurdeperr",
    "ffbb",
    "grib",
    "hhss",
    "hhzz",
    "hmore",
    "hydrs",
    "metar",
    "prognoz",
    "radiation",
    "sputn",
    "storm",
    "taf",
    "ttaa",
    "ttbb",
    "ttcc",
    "ttdd",
    "nogaps_grib2",
)

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
    """Отримує список файлів на Google Диску з їхнім SHA-256 та збереженою FTP-міткою"""
    existing = {}
    page_token = None
    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, size, sha256Checksum, appProperties)",
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
                "size": str(f.get("size", "")),
                "sha256": saved_hash.lower(),
                "ftp_mtime": props.get("ftp_mtime", ""),
            }
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return existing


def list_dir_fast(ftp, ftp_path):
    """Отримує список папок і файлів за 1 команду FTP без зайвих cwd для кожного файлу"""
    ftp.cwd(ftp_path)
    try:
        entries = []
        for name, facts in ftp.mlsd():
            if name in (".", ".."):
                continue
            is_dir = facts.get("type") == "dir"
            size = str(facts.get("size", ""))
            mtime = str(facts.get("modify", ""))
            entries.append((name, is_dir, size, mtime))
        return entries
    except Exception:
        lines = []
        ftp.retrlines("LIST", lines.append)
        entries = []
        for line in lines:
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[-1]
            if name in (".", ".."):
                continue
            is_dir = line.startswith("d") or "<DIR>" in line
            # У Unix LIST розмір зазвичай у parts[4], у Windows — у parts[2]
            size = ""
            if not is_dir:
                for token in (parts[4] if len(parts) > 4 else "", parts[2]):
                    if token.isdigit():
                        size = token
                        break
            entries.append((name, is_dir, size, ""))
        return entries


def collect_ftp_tasks(ftp, ftp_path, drive, drive_folder_id, tasks):
    """Швидко обходить папки FTP та відбирає тільки нові або потенційно змінені файли"""
    try:
        entries = list_dir_fast(ftp, ftp_path)
    except ftplib.error_perm as e:
        print(f"Папка {ftp_path} ще не створена на FTP: {e}", flush=True)
        return

    existing_files = get_existing_files(drive, drive_folder_id)

    for name, is_dir, ftp_size, mlsd_mtime in entries:
        full_ftp_path = f"{ftp_path}/{name}" if ftp_path != "/" else f"/{name}"
        if is_dir:
            sub_folder_id = get_or_create_folder(drive, name, drive_folder_id)
            collect_ftp_tasks(ftp, full_ftp_path, drive, sub_folder_id, tasks)
        else:
            if not is_target_file(name):
                continue

            existing_info = existing_files.get(name)
            # Якщо MLSD вже дав точний час і розмір, і вони збігаються — не додаємо навіть у чергу перевірки
            if (
                existing_info
                and mlsd_mtime
                and existing_info["ftp_mtime"] == mlsd_mtime
                and ftp_size
                and existing_info["size"] == ftp_size
            ):
                continue

            tasks.append({
                "ftp_path": ftp_path,
                "full_ftp_path": full_ftp_path,
                "name": name,
                "ftp_size": ftp_size,
                "mlsd_mtime": mlsd_mtime,
                "drive_folder_id": drive_folder_id,
                "existing_info": existing_info,
            })


def check_and_download_chunk(task_batch):
    """Перевіряє точну дату MDTM і завантажує файл ТІЛЬКИ якщо він новий або змінився"""
    results = []
    ftp = None
    try:
        ftp = connect_ftp()
        current_cwd = ""
        for task in task_batch:
            for attempt in range(1, 4):
                try:
                    if current_cwd != task["ftp_path"]:
                        ftp.cwd(task["ftp_path"])
                        current_cwd = task["ftp_path"]

                    # Отримуємо точну секунду зміни файлу на сервері (команда MDTM)
                    ftp_mtime = task["mlsd_mtime"]
                    if not ftp_mtime:
                        try:
                            resp = ftp.sendcmd(f"MDTM {task['name']}")
                            if resp.startswith("213 "):
                                ftp_mtime = resp.split()[1].strip()
                        except Exception:
                            ftp_mtime = ""

                    existing_info = task["existing_info"]
                    # Якщо файл вже є на Диску, і збігаються І розмір, І точна дата MDTM — не скачуємо!
                    if (
                        existing_info
                        and ftp_mtime
                        and existing_info["ftp_mtime"] == ftp_mtime
                        and (not task["ftp_size"] or existing_info["size"] == task["ftp_size"])
                    ):
                        break

                    # Завантажуємо тільки новий або змінений файл
                    bio = io.BytesIO()
                    ftp.retrbinary(f"RETR {task['name']}", bio.write)
                    file_bytes = bio.getvalue()
                    current_sha256 = hashlib.sha256(file_bytes).hexdigest().lower()
                    results.append((task, file_bytes, current_sha256, ftp_mtime))
                    break
                except Exception as e:
                    current_cwd = ""
                    if attempt == 3:
                        print(f"Не вдалося обробити {task['full_ftp_path']}: {e}", flush=True)
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

    print(f"Кандидатів на перевірку: {len(tasks)}", flush=True)

    num_workers = min(8, max(1, len(tasks)))
    batches = [tasks[i::num_workers] for i in range(num_workers)] if tasks else []

    downloaded_items = []
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(check_and_download_chunk, batch) for batch in batches]
        for future in as_completed(futures):
            downloaded_items.extend(future.result())

    total_changed = 0
    for task, file_bytes, current_sha256, ftp_mtime in downloaded_items:
        existing_info = task["existing_info"]

        # Якщо вміст SHA-256 не змінився — не чіпаємо файл на Диску (щоб зберегти дату змінення)
        if existing_info and existing_info["sha256"] == current_sha256:
            continue

        bio = io.BytesIO(file_bytes)
        media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)
        app_props = {
            "content_sha256": current_sha256,
            "ftp_mtime": ftp_mtime,
        }

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
