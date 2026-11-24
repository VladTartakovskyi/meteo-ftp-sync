import os
import io
import re
import hashlib
import socket
import ftplib
import time
import openpyxl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

socket.setdefaulttimeout(30)

FTP_SHEET_ID = os.environ["FTP_SHEET_ID"]
ROOT_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]

KYIV_TZ = timezone(timedelta(hours=2))


def is_truthy(cell_value):
    """Перевіряє, чи встановлено TRUE у клітинці (прапорець або спадний список)"""
    if isinstance(cell_value, bool):
        return cell_value
    if cell_value is None:
        return False
    return str(cell_value).strip().upper() == "TRUE"


def is_target_file(filename, download_all, target_prefixes):
    """Перевіряє, чи підходить файл під правила зі стовпчиків H та I"""
    if download_all:
        return True
    name_low = filename.lower()
    return name_low.startswith(target_prefixes)


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


def clean_ftp_host(raw_value):
    """Очищає посилання виду 'ftp://ftp.meteo.gov.ua/' до чистого хоста 'ftp.meteo.gov.ua'"""
    val = str(raw_value or "").strip()
    val = re.sub(r"^ftps?://", "", val, flags=re.IGNORECASE)
    val = val.split("/")[0].strip()
    return val


def get_sheet_settings(drive):
    """Зчитує налаштування FTP (C2:E3) та правила фільтрації зі стовпчиків H та I (від 2-го рядка)"""
    xlsx_bytes = (
        drive.files()
        .export(
            fileId=FTP_SHEET_ID,
            mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        .execute(num_retries=5)
    )
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)

    if "FTP" in wb.sheetnames:
        sheet = wb["FTP"]
    elif len(wb.worksheets) >= 3:
        sheet = wb.worksheets[2]
    else:
        raise ValueError("У Google Таблиці не знайдено аркуш 'FTP' або 3-й аркуш")

    configs = []
    for row_idx, label in ((2, "основний"), (3, "резервний")):
        host = clean_ftp_host(sheet[f"C{row_idx}"].value)
        user = str(sheet[f"D{row_idx}"].value or "").strip()
        password = str(sheet[f"E{row_idx}"].value or "").strip()

        if host and user and password:
            configs.append({
                "label": label,
                "host": host,
                "user": user,
                "pass": password,
            })

    if not configs:
        raise ValueError("У клітинках C2:E2 та C3:E3 відсутні коректні дані для підключення до FTP")

    # Зчитуємо стовпчик H (назви/префікси) та стовпчик I (TRUE/FALSE), починаючи з 2-го рядка
    download_all = False
    prefixes = []

    for row_idx in range(2, sheet.max_row + 1):
        raw_pattern = sheet[f"H{row_idx}"].value
        raw_flag = sheet[f"I{row_idx}"].value

        if raw_pattern is None:
            continue

        pattern = str(raw_pattern).strip()
        if not pattern:
            continue

        if not is_truthy(raw_flag):
            continue

        if pattern in ("*.*", "*"):
            download_all = True
        else:
            pat_low = pattern.lower()
            if pat_low not in prefixes:
                prefixes.append(pat_low)

    if download_all:
        print("Режим фільтрації: завантажуються УСІ файли (*.* = TRUE)", flush=True)
    else:
        print(f"Режим фільтрації за префіксами зі стовпчика H (активних TRUE: {len(prefixes)}): {prefixes}", flush=True)

    return configs, download_all, tuple(prefixes)


def connect_to_ftp(cfg, timeout=20):
    ftp = ftplib.FTP(cfg["host"], timeout=timeout)
    ftp.login(cfg["user"], cfg["pass"])
    ftp.set_pasv(True)
    return ftp


def get_working_ftp(ftp_configs, max_rounds=3, retry_delay=5):
    """Перевіряє основний і резервний FTP-сервери з повторними спробами при тимчасових збоях DNS/мережі"""
    last_error = None
    for round_num in range(1, max_rounds + 1):
        for cfg in ftp_configs:
            try:
                ftp = connect_to_ftp(cfg, timeout=15)
                print(f"Підключено до FTP ({cfg['label']}): {cfg['host']}", flush=True)
                return ftp, cfg
            except Exception as e:
                print(
                    f"[Спроба {round_num}/{max_rounds}] Сервер {cfg['host']} ({cfg['label']}) недоступний ({e}), пробую наступний...",
                    flush=True,
                )
                last_error = e
        if round_num < max_rounds:
            time.sleep(retry_delay)

    raise ConnectionError(f"Жоден із FTP-серверів не відповідає після {max_rounds} спроб. Остання помилка: {last_error}")


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
            size = ""
            if not is_dir:
                for token in (parts[4] if len(parts) > 4 else "", parts[2]):
                    if token.isdigit():
                        size = token
                        break
            entries.append((name, is_dir, size, ""))
        return entries


def collect_ftp_tasks(ftp, ftp_path, drive, drive_folder_id, download_all, target_prefixes, tasks):
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
            collect_ftp_tasks(ftp, full_ftp_path, drive, sub_folder_id, download_all, target_prefixes, tasks)
        else:
            if not is_target_file(name, download_all, target_prefixes):
                continue

            existing_info = existing_files.get(name)
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


def check_and_download_chunk(task_batch, active_cfg):
    """Перевіряє точну дату MDTM і завантажує файл ТІЛЬКИ якщо він новий або змінився"""
    results = []
    ftp = None
    try:
        ftp = connect_to_ftp(active_cfg)
        current_cwd = ""
        for task in task_batch:
            for attempt in range(1, 4):
                try:
                    if current_cwd != task["ftp_path"]:
                        ftp.cwd(task["ftp_path"])
                        current_cwd = task["ftp_path"]

                    ftp_mtime = task["mlsd_mtime"]
                    if not ftp_mtime:
                        try:
                            resp = ftp.sendcmd(f"MDTM {task['name']}")
                            if resp.startswith("213 "):
                                ftp_mtime = resp.split()[1].strip()
                        except Exception:
                            ftp_mtime = ""

                    existing_info = task["existing_info"]
                    if (
                        existing_info
                        and ftp_mtime
                        and existing_info["ftp_mtime"] == ftp_mtime
                        and (not task["ftp_size"] or existing_info["size"] == task["ftp_size"])
                    ):
                        break

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
                        ftp = connect_to_ftp(active_cfg)
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
    ftp_configs, download_all, target_prefixes = get_sheet_settings(drive)

    if not download_all and not target_prefixes:
        print("У стовпчику I немає жодного активного правила (TRUE). Перевірку завершено.", flush=True)
        return

    ftp, active_cfg = get_working_ftp(ftp_configs)
    tasks = []

    try:
        for target_date in dates_to_check:
            month_folder = target_date.strftime("%Y_%m")
            day_folder = target_date.strftime("%d_%m")
            month_id = get_or_create_folder(drive, month_folder, ROOT_FOLDER_ID)
            day_id = get_or_create_folder(drive, day_folder, month_id)
            collect_ftp_tasks(ftp, f"/{day_folder}", drive, day_id, download_all, target_prefixes, tasks)
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
        futures = [
            executor.submit(check_and_download_chunk, batch, active_cfg)
            for batch in batches
        ]
        for future in as_completed(futures):
            downloaded_items.extend(future.result())

    total_changed = 0
    for task, file_bytes, current_sha256, ftp_mtime in downloaded_items:
        existing_info = task["existing_info"]

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

    print(f"Перевірку завершено ({active_cfg['host']}). Додано або оновлено файлів: {total_changed}", flush=True)


if __name__ == "__main__":
    main()
