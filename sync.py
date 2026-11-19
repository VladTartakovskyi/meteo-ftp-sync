import os
import io
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

# Переносимо та оновлюємо лише файли, у назві яких є ці слова (без урахування регістру)
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
    """Повертає словник {ім'я_файлу: {'id': ..., 'ftp_mdtm': ..., 'ftp_size': ...}}"""
    existing = {}
    page_token = None
    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name, size, appProperties)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute(num_retries=5)
        )
        for f in response.get("files", []):
            props = f.get("appProperties") or {}
            existing[f["name"]] = {
                "id": f["id"],
                "ftp_mdtm": props.get("ftp_mdtm", ""),
                "ftp_size": str(props.get("ftp_size") or f.get("size") or ""),
            }
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return existing


def get_ftp_file_signature(ftp_ref, ftp_path, item):
    """Отримує точний час модифікації (MDTM) та розмір (SIZE) файлу на FTP"""
    for attempt in range(1, 4):
        try:
            ftp_ref[0].cwd(ftp_path)
            mdtm = ""
            try:
                resp = ftp_ref[0].sendcmd(f"MDTM {item}")
                if resp.startswith("213 "):
                    mdtm = resp[4:].strip()
            except Exception:
                mdtm = ""

            size = ""
            try:
                sz = ftp_ref[0].size(item)
                if sz is not None:
                    size = str(sz)
            except Exception:
                size = ""

            return mdtm, size
        except Exception as e:
            print(f"Повтор перевірки метаданих {item} (спроба {attempt}/3): {e}", flush=True)
            try:
                ftp_ref[0].quit()
            except Exception:
                pass
            time.sleep(2)
            ftp_ref[0] = connect_ftp()
    return None, None


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
    try:
        ftp_ref[0].cwd(ftp_path)
    except ftplib.error_perm as e:
        print(f"Папка {ftp_path} ще не створена на FTP: {e}", flush=True)
        return 0

    items = ftp_ref[0].nlst()
    for item in items:
        if item in (".", ".."):
            continue
        full_ftp_path = f"{ftp_path}/{item}" if ftp_path != "/" else f"/{item}"
        try:
            ftp_ref[0].cwd(full_ftp_path)
            sub_folder_id = get_or_create_folder(drive, item, drive_folder_id)
            sub_existing = get_existing_files(drive, sub_folder_id)
            changed_count += sync_directory(ftp_ref, full_ftp_path, drive, sub_folder_id, sub_existing)
            ftp_ref[0].cwd(ftp_path)
        except ftplib.error_perm:
            # 1. Фільтр: працюємо лише зі Storm, AVIA, HydrS та AADD
            if not any(keyword in item.lower() for keyword in ALLOWED_FILES):
                continue

            # 2. Перевіряємо дату зміни та розмір файлу на FTP
            ftp_mdtm, ftp_size = get_ftp_file_signature(ftp_ref, ftp_path, item)
            if ftp_mdtm is None:
                continue

            existing_info = existing_files.get(item)

            # Якщо файл уже є на Диску — порівнюємо його сигнатуру з FTP
            if existing_info:
                same_mdtm = bool(ftp_mdtm) and (existing_info["ftp_mdtm"] == ftp_mdtm)
                same_size = bool(ftp_size) and (existing_info["ftp_size"] == ftp_size)

                # Якщо і дата модифікації, і розмір не змінилися — залишаємо файл без змін
                if same_mdtm and same_size:
                    continue
                # Якщо сервер не підтримує MDTM, перевіряємо хоча б розмір
                if not ftp_mdtm and same_size:
                    continue

            # 3. Завантажуємо новий або оновлений файл із FTP
            bio = download_from_ftp(ftp_ref, ftp_path, item)
            if bio is None:
                print(f"Пропущено після 3 невдалих спроб FTP: {item}", flush=True)
                continue

            actual_size = str(bio.getbuffer().nbytes)
            app_props = {
                "ftp_mdtm": ftp_mdtm,
                "ftp_size": ftp_size or actual_size,
            }
            media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)

            for attempt in range(1, 6):
                try:
                    if existing_info:
                        # Оновлюємо вміст існуючого файлу на Диску новішою версією
                        drive.files().update(
                            fileId=existing_info["id"],
                            body={"appProperties": app_props},
                            media_body=media,
                            fields="id",
                        ).execute(num_retries=5)
                        print(f"Оновлено (нова версія): {full_ftp_path}", flush=True)
                    else:
                        # Створюємо новий файл на Диску
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
                            "ftp_mdtm": ftp_mdtm,
                            "ftp_size": ftp_size or actual_size,
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
    dates_to_check = [now - timedelta(days=1), now]
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
