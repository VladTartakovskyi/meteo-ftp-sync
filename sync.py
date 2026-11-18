import ftplib
import io
import json
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

FTP_HOST = "ftp.meteo.gov.ua"
FTP_USER = os.environ["FTP_USER"]
FTP_PASS = os.environ["FTP_PASS"]
ROOT_FOLDER_ID = os.environ["GDRIVE_FOLDER_ID"]
CREDS_JSON = json.loads(os.environ["GDRIVE_CREDENTIALS"])

def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        CREDS_JSON, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def get_or_create_folder(service, folder_name, parent_id):
    query = (
        f"name = '{folder_name}' and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
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

def sync_directory(ftp, remote_path, drive, drive_folder_id):
    ftp.cwd(remote_path)
    
    # Отримуємо перелік наявних файлів у поточній папці Google Диску
    query = f"'{drive_folder_id}' in parents and trashed = false"
    results = drive.files().list(q=query, pageSize=1000, fields="files(name)").execute()
    existing_names = {f["name"] for f in results.get("files", [])}

    entries = []
    try:
        for name, facts in ftp.mlsd():
            if name not in [".", ".."]:
                entries.append((name, facts.get("type", "file")))
    except Exception:
        for name in ftp.nlst():
            entries.append((name, "unknown"))

    uploaded_count = 0
    for name, item_type in entries:
        safe_name = re.sub(r'[=<>:"/\\|?*]', '_', name)
        
        if item_type == "dir":
            sub_drive_id = get_or_create_folder(drive, safe_name, drive_folder_id)
            uploaded_count += sync_directory(ftp, f"{remote_path}/{name}", drive, sub_drive_id)
            ftp.cwd(remote_path)
        else:
            if safe_name in existing_names:
                continue
            try:
                buffer = io.BytesIO()
                ftp.retrbinary(f"RETR {name}", buffer.write)
                buffer.seek(0)

                file_metadata = {"name": safe_name, "parents": [drive_folder_id]}
                media = MediaIoBaseUpload(buffer, mimetype="application/octet-stream", resumable=True)
                drive.files().create(body=file_metadata, media_body=media, fields="id").execute()
                
                uploaded_count += 1
                print(f"Перенесено: {safe_name}")
            except ftplib.error_perm:
                # Якщо сервер не підтримував mlsd і це виявилась папка
                sub_drive_id = get_or_create_folder(drive, safe_name, drive_folder_id)
                uploaded_count += sync_directory(ftp, f"{remote_path}/{name}", drive, sub_drive_id)
                ftp.cwd(remote_path)

    return uploaded_count

def main():
    now = datetime.now(ZoneInfo("Europe/Kyiv"))
    today_folder = now.strftime("%d_%m")      # Наприклад: 17_11
    year_month_folder = now.strftime("%Y_%m") # Наприклад: 2026_11
    
    print(f"Старт перевірки за {now.strftime('%Y-%m-%d %H:%M')} (шукаємо /{today_folder})")

    ftp = ftplib.FTP(FTP_HOST, timeout=30)
    ftp.login(user=FTP_USER, passwd=FTP_PASS)
    ftp.set_pasv(True)
    ftp.encoding = "utf-8"

    try:
        ftp.cwd(f"/{today_folder}")
    except ftplib.error_perm:
        print(f"Папки /{today_folder} на FTP-сервері ще немає.")
        ftp.quit()
        return

    drive = get_drive_service()
    ym_id = get_or_create_folder(drive, year_month_folder, ROOT_FOLDER_ID)
    day_id = get_or_create_folder(drive, today_folder, ym_id)
    
    total_uploaded = sync_directory(ftp, f"/{today_folder}", drive, day_id)
    ftp.quit()
    print(f"Завершено. Нових файлів перенесено: {total_uploaded}")

if __name__ == "__main__":
    main()
