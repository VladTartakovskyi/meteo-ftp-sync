import os
import io
import ftplib
import time
from datetime import datetime, timezone, timedelta
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError

FTP_HOST = "ftp.meteo.gov.ua"
FTP_USER = os.environ["FTP_USER"]
FTP_PASS = os.environ["FTP_PASS"]
ROOT_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]

KYIV_TZ = timezone(timedelta(hours=2))


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
    print(f"Створено папку: {name}")
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


def sync_directory(ftp, ftp_path, drive, drive_folder_id, existing_files):
    uploaded_count = 0
    try:
        ftp.cwd(ftp_path)
    except ftplib.error_perm as e:
        print(f"Не вдалося відкрити {ftp_path}: {e}")
        return 0

    items = ftp.nlst()
    for item in items:
        if item in (".", ".."):
            continue
        full_ftp_path = f"{ftp_path}/{item}" if ftp_path != "/" else f"/{item}"
        try:
            ftp.cwd(full_ftp_path)
            sub_folder_id = get_or_create_folder(drive, item, drive_folder_id)
            sub_existing = get_existing_files(drive, sub_folder_id)
            uploaded_count += sync_directory(ftp, full_ftp_path, drive, sub_folder_id, sub_existing)
            ftp.cwd(ftp_path)
        except ftplib.error_perm:
            if item in existing_files:
                continue

            bio = io.BytesIO()
            ftp.retrbinary(f"RETR {item}", bio.write)
            bio.seek(0)

            file_metadata = {"name": item, "parents": [drive_folder_id]}
            media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)

            for attempt in range(1, 6):
                try:
                    drive.files().create(
                        body=file_metadata, media_body=media, fields="id"
                    ).execute(num_retries=5)
                    print(f"Перенесено: {full_ftp_path}")
                    existing_files.add(item)
                    uploaded_count += 1
                    break
                except Exception as err:
                    if attempt == 5:
                        print(f"Не вдалося завантажити {item} після 5 спроб: {err}")
                    else:
                        time.sleep(attempt * 3)

    return uploaded_count


def main():
    now = datetime.now(KYIV_TZ)
    month_folder = now.strftime("%Y_%m")
    day_folder = now.strftime("%d_%m")
    print(f"Старт перевірки за {now.strftime('%Y-%m-%d %H:%M')} (шукаємо /{day_folder})")

    drive = get_drive_service()
    month_id = get_or_create_folder(drive, month_folder, ROOT_FOLDER_ID)
    day_id = get_or_create_folder(drive, day_folder, month_id)
    existing_files = get_existing_files(drive, day_id)

    with ftplib.FTP(FTP_HOST, timeout=60) as ftp:
        ftp.login(FTP_USER, FTP_PASS)
        ftp.set_pasv(True)
        total_uploaded = sync_directory(ftp, f"/{day_folder}", drive, day_id, existing_files)

    print(f"Готово! Завантажено нових файлів: {total_uploaded}")


if __name__ == "__main__":
    main()
