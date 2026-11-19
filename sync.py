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

# Переносимо лише файли, у назві яких є ці слова (без урахування регістру)
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
    existing = set()
    page_token = None
    while True:
        query = f"'{folder_id}' in parents and trashed = false"
        response = (
            service.files()
            .list(
                q=query,
                fields="nextPageToken, files(name)",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute(num_retries=5)
        )
        for f in response.get("files", []):
            existing.add(f["name"])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return existing


def download_from_ftp(ftp_ref, ftp_path, item):
    for attempt in range(1, 4):
        try:
            ftp_ref[0].cwd(ftp_path)
            bio = io.BytesIO()
            ftp_ref[0].retrbinary(f"RETR {item}", bio.write)
            bio.seek(0)
            return bio
        except Exception as e:
            print(f"Повтор FTP для {item} (спроба {attempt}/3): {e}", flush=True)
            try:
                ftp_ref[0].quit()
            except Exception:
                pass
            time.sleep(2)
            ftp_ref[0] = connect_ftp()
    return None


def sync_directory(ftp_ref, ftp_path, drive, drive_folder_id, existing_files):
    uploaded_count = 0
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
            uploaded_count += sync_directory(ftp_ref, full_ftp_path, drive, sub_folder_id, sub_existing)
            ftp_ref[0].cwd(ftp_path)
        except ftplib.error_perm:
            # Пропускаємо всі файли, крім Storm, AVIA, HydrS та AADD
            if not any(keyword in item.lower() for keyword in ALLOWED_FILES):
                continue

            if item in existing_files:
                continue

            bio = download_from_ftp(ftp_ref, ftp_path, item)
            if bio is None:
                print(f"Пропущено після 3 невдалих спроб FTP: {item}", flush=True)
                continue

            file_metadata = {"name": item, "parents": [drive_folder_id]}
            media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)

            for attempt in range(1, 6):
                try:
                    drive.files().create(
                        body=file_metadata, media_body=media, fields="id"
                    ).execute(num_retries=5)
                    print(f"Перенесено: {full_ftp_path}", flush=True)
                    existing_files.add(item)
                    uploaded_count += 1
                    break
                except Exception as err:
                    if attempt == 5:
                        print(f"Не вдалося завантажити {item} на Диск: {err}", flush=True)
                    else:
                        time.sleep(attempt * 3)

    return uploaded_count


def main():
    now = datetime.now(KYIV_TZ)
    dates_to_check = [now - timedelta(days=1), now]
    print(f"Старт перевірки за {now.strftime('%Y-%m-%d %H:%M')} (фільтр: Storm, AVIA, HydrS, AADD)", flush=True)

    drive = get_drive_service()
    ftp_ref = [connect_ftp()]
    total_uploaded = 0

    try:
        for target_date in dates_to_check:
            month_folder = target_date.strftime("%Y_%m")
            day_folder = target_date.strftime("%d_%m")
            month_id = get_or_create_folder(drive, month_folder, ROOT_FOLDER_ID)
            day_id = get_or_create_folder(drive, day_folder, month_id)
            existing_files = get_existing_files(drive, day_id)
            print(f"Перевіряємо /{day_folder} (вже на Диску: {len(existing_files)})", flush=True)
            total_uploaded += sync_directory(ftp_ref, f"/{day_folder}", drive, day_id, existing_files)
    finally:
        try:
            ftp_ref[0].quit()
        except Exception:
            pass

    print(f"Готово! Завантажено нових файлів: {total_uploaded}", flush=True)


if __name__ == "__main__":
    main()
