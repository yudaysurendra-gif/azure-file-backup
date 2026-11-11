"""
Azure File Backup
-----------------
Back up a local folder to Azure Blob Storage, list what is stored,
restore files, and delete old backups.

Only new or changed files are uploaded (incremental backup), detected by MD5 hash.

SETUP
    pip install -r requirements.txt
    Copy .env.example to .env and add your storage account connection string.

USAGE
    python azure_file_backup.py backup  ./my_documents
    python azure_file_backup.py backup  ./my_documents --name project1
    python azure_file_backup.py list
    python azure_file_backup.py list    --name project1
    python azure_file_backup.py restore project1 ./restored_files
    python azure_file_backup.py delete  project1
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # .env support is optional; environment variables still work

DEFAULT_CONTAINER = "file-backup"


# ------------------------------------------------------------------ Helpers
def get_container_client():
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        sys.exit("Error: set AZURE_STORAGE_CONNECTION_STRING (see .env.example).")

    container_name = os.getenv("AZURE_CONTAINER_NAME", DEFAULT_CONTAINER)
    service = BlobServiceClient.from_connection_string(conn_str)
    container = service.get_container_client(container_name)
    try:
        container.create_container()
        print(f"Created container '{container_name}'.")
    except ResourceExistsError:
        pass
    return container


def md5_of_file(path, chunk_size=1024 * 1024):
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            md5.update(chunk)
    return md5.hexdigest()


def human_size(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


# ----------------------------------------------------------------- Commands
def backup(folder, name):
    source = Path(folder)
    if not source.is_dir():
        sys.exit(f"Error: '{folder}' is not a folder.")

    name = name or source.resolve().name
    container = get_container_client()

    # Existing blobs in this backup: {blob_name: md5}
    existing = {
        b.name: (b.metadata or {}).get("md5")
        for b in container.list_blobs(name_starts_with=f"{name}/", include=["metadata"])
    }

    uploaded = skipped = failed = 0
    total_bytes = 0

    for path in sorted(p for p in source.rglob("*") if p.is_file()):
        blob_name = f"{name}/{path.relative_to(source).as_posix()}"
        try:
            local_md5 = md5_of_file(path)
            if existing.get(blob_name) == local_md5:
                skipped += 1
                print(f"  skip    {blob_name}")
                continue
            with open(path, "rb") as f:
                container.upload_blob(
                    blob_name, f, overwrite=True, metadata={"md5": local_md5}
                )
            uploaded += 1
            total_bytes += path.stat().st_size
            print(f"  upload  {blob_name}")
        except Exception as exc:
            failed += 1
            print(f"  FAILED  {blob_name}: {exc}")

    print(
        f"\nBackup '{name}' finished: {uploaded} uploaded "
        f"({human_size(total_bytes)}), {skipped} unchanged, {failed} failed."
    )


def list_backups(name=None):
    container = get_container_client()

    if name:
        print(f"Files in backup '{name}':")
        count = 0
        for blob in container.list_blobs(name_starts_with=f"{name}/"):
            count += 1
            when = blob.last_modified.strftime("%Y-%m-%d %H:%M")
            print(f"  {blob.name[len(name) + 1:]:<50} {human_size(blob.size):>10}  {when}")
        if count == 0:
            print("  (nothing found)")
        return

    summary = {}
    for blob in container.list_blobs():
        backup_name = blob.name.split("/", 1)[0]
        info = summary.setdefault(backup_name, {"files": 0, "size": 0, "last": blob.last_modified})
        info["files"] += 1
        info["size"] += blob.size
        info["last"] = max(info["last"], blob.last_modified)

    if not summary:
        print("No backups found.")
        return
    print(f"{'BACKUP':<25}{'FILES':>7}{'SIZE':>12}   LAST UPDATED")
    for backup_name, info in sorted(summary.items()):
        print(
            f"{backup_name:<25}{info['files']:>7}{human_size(info['size']):>12}"
            f"   {info['last'].strftime('%Y-%m-%d %H:%M')}"
        )


def restore(name, destination):
    container = get_container_client()
    dest = Path(destination)
    count = 0

    for blob in container.list_blobs(name_starts_with=f"{name}/"):
        relative = blob.name[len(name) + 1:]
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(container.download_blob(blob.name).readall())
        count += 1
        print(f"  restored {relative}")

    if count == 0:
        sys.exit(f"No backup named '{name}' was found.")
    print(f"\nRestored {count} file(s) to '{dest.resolve()}'.")


def delete_backup(name):
    container = get_container_client()
    blobs = [b.name for b in container.list_blobs(name_starts_with=f"{name}/")]
    if not blobs:
        sys.exit(f"No backup named '{name}' was found.")

    answer = input(f"Delete backup '{name}' ({len(blobs)} files)? Type 'yes' to confirm: ")
    if answer.strip().lower() != "yes":
        print("Cancelled.")
        return
    for blob_name in blobs:
        container.delete_blob(blob_name)
    print(f"Deleted {len(blobs)} file(s).")


# --------------------------------------------------------------------- Main
def main():
    parser = argparse.ArgumentParser(description="Back up files to Azure Blob Storage.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backup", help="Upload a folder (new/changed files only)")
    p.add_argument("folder")
    p.add_argument("--name", help="Backup name (default: folder name)")

    p = sub.add_parser("list", help="List backups, or files inside one backup")
    p.add_argument("--name")

    p = sub.add_parser("restore", help="Download a backup to a local folder")
    p.add_argument("name")
    p.add_argument("destination")

    p = sub.add_parser("delete", help="Delete a backup from Azure")
    p.add_argument("name")

    args = parser.parse_args()

    if args.command == "backup":
        backup(args.folder, args.name)
    elif args.command == "list":
        list_backups(args.name)
    elif args.command == "restore":
        restore(args.name, args.destination)
    elif args.command == "delete":
        delete_backup(args.name)


if __name__ == "__main__":
    main()
