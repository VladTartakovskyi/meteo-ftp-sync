import os
import io
import hashlib
import socket
import ftplib
import time
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

ALLOWED_FILES = ("storm", "avia", "hydrs", "aadd")


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
    """Отримує список файлів на Диску разом із SHA-256 та збереженою FTP-сигнатурою"""
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
                "ftp_sig": props.get("ftp_sig", ""),
            }
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return existing


def list_ftp_entries(ftp_ref, ftp_path):
    """Швидко отримує весь вміст папки за 1 команду (імена, типи, дати та розміри)"""
    for attempt in range(1, 4):
        try:
            ftp_ref[0].cwd(ftp_path)
            # Спроба сучасного швидкого MLSD
            try:
                entries = []
                for name, facts in ftp_ref[0].mlsd():
                    if name in (".", ".."):
                        continue
                    etype = facts.get("type", "")
                    is_dir = etype == "dir"
                    sig = f"{facts.get('modify', '')}_{facts.get('size', '')}"
                    entries.append((name, is_dir, sig))
                return entries
            except Exception:
                # Резервний швидкий LIST за 1 запит на всю папку
                lines = []
                ftp_ref[0].retrlines("LIST", lines.append)
                entries = []
                for line in lines:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    name = parts[-1]
                    if name in (".", ".."):
                        continue
                    is_dir = line.startswith("d") or "<DIR>" in line
                    # Беремо розмір і дату з рядка LIST як швидку сигнатуру
                    sig = "_".join(parts[:-1])
                    entries.append((name, is_dir, sig))
                return entries
        except ftplib.error_perm as e:
            print(f"Папка {ftp_path} ще не створена на FTP: {e}", flush=True)
            return []
        except Exception as e:
            print(f"Повтор читання папки {ftp_path} (спроба {attempt}/3): {e}", flush=True)
            try:
                ftp_ref[0].quit()
            except Exception:
                pass
            time.sleep(2)
            ftp_ref[0] = connect_ftp()
    return []


def download_from_ftp(ftp_ref, ftp_path, item):
    for attempt in range(1, 4):
        try:
            ftp_ref[0].cwd(ftp_path)
            bio = io.BytesIO()
            ftp_ref[0].retrbinary(f"RETR {item}", bio.write)
            bio.seek(0)
            return bio
        except Exception as e:
            print(f"Повтор скачування {item} (спроба {attempt}/3): {e}", flush=True)
            try:
                ftp_ref[0].quit()
            except Exception:
                pass
            time.sleep(2)
            ftp_ref[0] = connect_ftp()
    return None


def sync_directory(ftp_ref, ftp_path, drive, drive_folder_id, existing_files):
    changed_count = 0
    entries = list_ftp_entries(ftp_ref, ftp_path)

    for item, is_dir, ftp_sig in entries:
        full_ftp_path = f"{ftp_path}/{item}" if ftp_path != "/" else f"/{item}"
        if is_dir:
            sub_folder_id = get_or_create_folder(drive, item, drive_folder_id)
            sub_existing = get_existing_files(drive, sub_folder_id)
            changed_count += sync_directory(ftp_ref, full_ftp_path, drive, sub_folder_id, sub_existing)
        else:
            if not any(keyword in item.lower() for keyword in ALLOWED_FILES):
                continue

            existing_info = existing_files.get(item)

            # 1. Миттєва перевірка без завантаження: якщо сигнатура FTP не змінилася — пропускаємо
            if existing_info and existing_info["ftp_sig"] == ftp_sig and ftp_sig != "":
                continue

            # 2. Завантажуємо тільки новий або змінений на FTP файл
            bio = download_from_ftp(ftp_ref, ftp_path, item)
            if bio is None:
                print(f"Пропущено після 3 невдалих спроб FTP: {item}", flush=True)
                continue

            file_bytes = bio.getvalue()
            current_sha256 = hashlib.sha256(file_bytes).hexdigest().lower()
            bio.seek(0)

            app_props = {
                "content_sha256": current_sha256,
                "ftp_sig": ftp_sig,
            }

            # 3. Якщо на FTP оновилася дата, але вміст (SHA-256) лишився ідентичним —
            # оновлюємо лише технічну мітку без перезапису самого файлу
            if existing_info and existing_info["sha256"] == current_sha256:
                try:
                    drive.files().update(
                        fileId=existing_info["id"],
                        body={"appProperties": app_props},
                        fields="id",
                    ).execute(num_retries=5)
                except Exception:
                    pass
                continue

            media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)

            for attempt in range(1, 6):
                try:
                    if existing_info:
                        drive.files().update(
                            fileId=existing_info["id"],
                            body={"appProperties": app_props},
                            media_body=media,
                            fields="id",
                        ).execute(num_retries=5)
                        print(f"Оновлено (змінився вміст): {full_ftp_path}", flush=True)
                    else:
                        file_metadata = {
                            "name": item,
                            "parents": [drive_folder_id],
                            "appProperties": app_props,
                        }
                        created = drive.files().create(
                            body=file_metadata, media_body=media, fields="id"
                        ).execute(num_retries=5)
                        existing_files[item] = {
                            "id": created["id"],
                            "sha256": current_sha256,
                            "ftp_sig": ftp_sig,
                        }
                        print(f"Додано новий: {full_ftp_path}", flush=True)

                    changed_count += 1
                    break
                except Exception as err:
                    if attempt == 5:
                        print(f"Помилка запису {item} на Диск: {err}", flush=True)
                    else:
                        time.sleep(attempt * 3)

    return changed_count


def main():
    now = datetime.now(KYIV_TZ)
    # Вчорашню папку перевіряємо лише в перші 2 години після опівночі, решту дня — тільки сьогоднішню
    if now.hour < 2:
        dates_to_check = [now - timedelta(days=1), now]
    else:
        dates_to_check = [now]

    print(f"Старт перевірки за {now.strftime('%Y-%m-%d %H:%M')}", flush=True)

    drive = get_drive_service()
    ftp_ref = [connect_ftp()]
    total_changed = 0

    try:
        for target_date in dates_to_check:
            month_folder = target_date.strftime("%Y_%m")
            day_folder = target_date.strftime("%d_%m")
            month_id = get_or_create_folder(drive, month_folder, ROOT_FOLDER_ID)
            day_id = get_or_create_folder(drive, day_folder, month_id)
            existing_files = get_existing_files(drive, day_id)
            total_changed += sync_directory(ftp_ref, f"/{day_folder}", drive, day_id, existing_files)
    finally:
        try:
            ftp_ref[0].quit()
        except Exception:
            pass

    print(f"Перевірку завершено. Додано або оновлено файлів: {total_changed}", flush=True)


if __name__ == "__main__":
    main()
