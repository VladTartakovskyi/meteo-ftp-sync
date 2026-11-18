import ftplib
import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

FTP_HOST = "ftp.meteo.gov.ua"
FTP_USER = os.environ["FTP_USER"]
FTP_PASS = os.environ["FTP_PASS"]
ROOT_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)

def get_or_create_folder(service, folder_name, parent_id):
    query = (
        f"name = '{folder_name}' and "
        f"'{parent_id}' in parents and "
        f"mimeType = 'application/vnd.google-apps.folder' and "
        f"trashed = false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    file_metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=file_metadata, fields="id").execute()
    print(f"Створено папку: {folder_name}")
    return folder.get("id")

def get_existing_files(service, folder_id):
    query = f"'{folder_id}' in parents and trashed = false"
    results = service.files().list(q=query, pageSize=1000, fields="files(name)").execute()
    return {f["name"] for f in results.get("files", [])}

def sync_directory(ftp, ftp_path, drive, drive_folder_id):
    existing_files = get_existing_files(drive, drive_folder_id)
    uploaded_count = 0

    try:
        items = ftp.nlst(ftp_path)
    except ftplib.error_perm as e:
        print(f"Папку {ftp_path} не знайдено на FTP: {e}")
        return 0

    for item in items:
        name = os.path.basename(item.rstrip("/"))
        if name in (".", "..", ""):
            continue

        full_ftp_path = f"{ftp_path}/{name}"

        try:
            ftp.cwd(full_ftp_path)
            ftp.cwd("/")
            sub_folder_id = get_or_create_folder(drive, name, drive_folder_id)
            uploaded_count += sync_directory(ftp, full_ftp_path, drive, sub_folder_id)
        except ftplib.error_perm:
            if name in existing_files:
                continue

            bio = io.BytesIO()
            ftp.retrbinary(f"RETR {full_ftp_path}", bio.write)
            bio.seek(0)

            file_metadata = {"name": name, "parents": [drive_folder_id]}
            media = MediaIoBaseUpload(bio, mimetype="application/octet-stream", resumable=True)
            drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
            print(f"Перенесено: {full_ftp_path}")
            uploaded_count += 1

    return uploaded_count

def main():
    now_kyiv = datetime.now(ZoneInfo("Europe/Kyiv"))
    month_folder_name = now_kyiv.strftime("%Y_%m")
    today_folder = now_kyiv.strftime("%d_%m")

    print(f"Старт перевірки за {now_kyiv.strftime('%Y-%m-%d %H:%M')} (шукаємо /{today_folder})")

    drive = get_drive_service()
    month_id = get_or_create_folder(drive, month_folder_name, ROOT_FOLDER_ID)
    day_id = get_or_create_folder(drive, today_folder, month_id)

    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.encoding = "utf-8"

    total_uploaded = sync_directory(ftp, f"/{today_folder}", drive, day_id)
    ftp.quit()
    print(f"Готово. Нових файлів перенесено: {total_uploaded}")

if __name__ == "__main__":
    main()
