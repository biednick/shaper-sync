#!/usr/bin/env python3
"""
Synchronize a local directory to Shaper Hub (hub.shapertools.com).

Usage:
    python shaper_sync.py <directory> [--email EMAIL] [--password PASSWORD]
                                      [--remote-path /remote/path]
                                      [--dry-run] [--watch] [--verbose]

Credentials can also be provided via the environment variables
SHAPER_EMAIL and SHAPER_PASSWORD.
"""

import argparse
import fnmatch
import logging
import sys
import threading
from collections import Counter
from datetime import datetime, time, timezone
import time as t
from os import environ
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import requests

logger = logging.getLogger("shaper_sync")

AUTH_URL = "https://auth.shapertools.com"
API_URL = "https://api.shapertools.com"
HUB_ORIGIN = "https://hub.shapertools.com"

# Headers required by the Shaper API.
# - Origin is checked server-side for CORS validation.
# - X-ApiVersion matches the version used by the official Shaper Studio app.
COMMON_HEADERS = {
    "Origin": HUB_ORIGIN,
    "Referer": f"{HUB_ORIGIN}/",
    "X-ApiVersion": "3.0.0",
}



class ShaperHubClient:
    """Client for the undocumented Shaper Hub API, reverse-engineered from
    the Shaper Studio web app (studio.shapertools.com)."""

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self.session = requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        self._authenticate()

    def _tree_url(self, path: str, name: str) -> str:
        """Build the URL for a file/folder entry in the userspace tree."""
        parent = path if path.endswith("/") else path + "/"
        return f"{API_URL}/files/userspace/tree/{parent}{name}"

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Send a request, re-authenticating once on 401."""
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.info("Token expired, re-authenticating...")
            self._authenticate()
            resp = self.session.request(method, url, **kwargs)
        return resp

    def _authenticate(self) -> None:
        """Obtain a JWT via POST /token and set it as Bearer token."""
        logger.info("Authenticating...")
        self.session.headers.pop("Authorization", None)
        resp = self.session.post(
            f"{AUTH_URL}/token",
            json={
                "client_id": "000000000000000000000000",
                "grant_type": "password",
                "username": self._email,
                "password": self._password,
                "scope": "*",
                "acceptTC": False,
            },
            headers={"X-ApiVersion": "2.0.0"},
        )
        if resp.status_code != 200:
            body = resp.text
            try:
                body = resp.json().get("message", body)
            except Exception:
                pass
            logger.error("Authentication error (%d): %s", resp.status_code, body)
            sys.exit(1)

        data = resp.json()
        # Response format: {"access_token": {"token": "jwt"}, "expires": "...", ...}
        try:
            token = data["access_token"]["token"]
        except (KeyError, TypeError):
            logger.error("Unable to extract token. Response: %s", data)
            sys.exit(1)

        self.session.headers["Authorization"] = f"Bearer {token}"
        logger.info("Authentication successful.")

    def list_files(
        self, path: str = "/", file_type: str | None = None, limit: int = 200
    ) -> list[dict]:
        """List files in the user's personal space at the given path."""
        params = {
            "spaceType": "userspace",
            "limit": str(limit),
            "path": path if path.endswith("/") else path + "/",
            "sort": "modified:-1",
        }
        if file_type:
            params["type"] = file_type
        resp = self._request("GET", f"{API_URL}/files/userspace/search", params=params)
        resp.raise_for_status()
        return resp.json().get("results", [])

    def create_folder(self, path: str, name: str) -> dict:
        """Create a folder in the user's personal space."""
        resp = self._request("POST", self._tree_url(path, name), json={"type": "folder"})
        resp.raise_for_status()
        return resp.json()

    def create_file_entry(self, path: str, name: str, blob_id: str) -> dict:
        """Create a file entry linked to a blob in the user's personal space."""
        resp = self._request(
            "POST", self._tree_url(path, name),
            json={"type": "file", "blobs": [blob_id]},
        )
        resp.raise_for_status()
        return resp.json()

    def delete_file(self, path: str, name: str) -> None:
        """Delete a file entry from the user's personal space."""
        resp = self._request("DELETE", self._tree_url(path, name))
        resp.raise_for_status()

    def upload_blob(self, file_path: Path) -> str:
        """Upload raw file bytes to blob storage and return the blob ID."""
        with open(file_path, "rb") as f:
            resp = self._request(
                "POST", f"{API_URL}/blobs/",
                data=f,
                headers={"Content-Type": "application/octet-stream"},
            )
        resp.raise_for_status()
        data = resp.json()
        logger.debug("Blob upload response: %s", data)
        return data["blobs"][0]

    def _upload_file(self, remote_path: str, entry: Path) -> str:
        """Upload blob + create file entry. Returns the blob ID."""
        blob_id = self.upload_blob(entry)
        self.create_file_entry(remote_path, entry.name, blob_id)
        return blob_id

    def sync_file(self, local_file: Path, remote_path: str) -> None:
        """Upload or update a single file on Shaper Hub.

        Checks whether the file already exists remotely and either uploads
        it as new or deletes + re-uploads if the local version is newer.
        """
        remote_path = remote_path.rstrip("/") + "/" if remote_path != "/" else "/"
        self.ensure_remote_path(remote_path)
        remote_files = self.get_remote_files(remote_path)

        if local_file.name in remote_files:
            logger.info("Updating: %s...", local_file.name)
            self.delete_file(remote_path, local_file.name)
        else:
            logger.info("Uploading: %s...", local_file.name)

        blob_id = self._upload_file(remote_path, local_file)
        logger.info("OK: %s (blob: %s)", local_file.name, blob_id)

    def ensure_remote_path(self, remote_path: str) -> None:
        """Recursively create remote folders if they don't exist yet."""
        if remote_path == "/":
            return
        parts = [p for p in remote_path.strip("/").split("/") if p]
        current = "/"
        for part in parts:
            existing = {
                item["name"] for item in self.list_files(current, file_type="folder")
            }
            if part not in existing:
                logger.debug("Creating folder: %s%s/", current, part)
                self.create_folder(current, part)
            current = f"{current}{part}/"

    def get_remote_files(self, remote_path: str) -> dict[str, datetime]:
        """Return a mapping of filename -> modified datetime for a remote folder."""
        return {
            f["name"]: datetime.fromisoformat(f["modified"].replace("Z", "+00:00"))
            for f in self.list_files(remote_path, file_type="file")
        }

    def download_file(self, remote_entry: dict, local_dir: Path) -> None:
        """Download a remote file entry to local_dir.

        GET /blobs/{id} returns a 303 redirect to a presigned S3 URL.
        requests follows the redirect automatically, so we stream the
        final response body directly to disk.
        """
        name = remote_entry["name"]
        blob_id = remote_entry["blobs"][0]
        resp = self._request(
            "GET", f"{API_URL}/blobs/{blob_id}",
            stream=True, allow_redirects=True,
        )
        logger.debug(
            "GET /blobs/%s -> %d %s (via %d redirect(s))",
            blob_id, resp.status_code, resp.reason, len(resp.history),
        )
        resp.raise_for_status()
        dest = local_dir / name
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        logger.info("Downloaded: %s", name)

    def download_directory(
        self,
        local_dir: Path,
        remote_path: str = "/",
        *,
        dry_run: bool = False,
        recursive: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> Counter:
        """Download files from Shaper Hub that are not present locally.

        Returns a Counter with keys: downloaded, skipped, errors.
        """
        stats: Counter = Counter()
        remote_path = remote_path.rstrip("/") + "/" if remote_path != "/" else "/"

        if recursive:
            for folder in self.list_files(remote_path, file_type="folder"):
                name = folder["name"]
                if not self._is_valid_windows_name(name):
                    logger.warning("Skipping folder with unsupported name: %r", name)
                    continue
                logger.info("Directory: %s/", name)
                sub_local = local_dir / name
                if not dry_run:
                    sub_local.mkdir(exist_ok=True)
                stats += self.download_directory(
                    sub_local,
                    f"{remote_path}{name}/",
                    dry_run=dry_run,
                    recursive=True,
                    include=include,
                    exclude=exclude,
                )

        remote_entries = self.list_files(remote_path, file_type="file")
        logger.debug("Remote entries: %s", [e["name"] for e in remote_entries])
        local_names = {p.name for p in local_dir.iterdir() if p.is_file()} if not dry_run else set()

        to_download = [
            e for e in remote_entries
            if self._file_matches(e["name"], include, exclude) and e["name"] not in local_names
        ]
        total = len(to_download)
        logger.info("Files to download: %d", total)

        for entry in remote_entries:
            name = entry["name"]
            if not self._is_valid_windows_name(name):
                logger.warning("Skipping file with unsupported name: %r", name)
                continue
            if not self._file_matches(name, include, exclude):
                logger.debug("Filtered out: %s", name)
                continue
            if name in local_names:
                logger.info("Skipped (already local): %s", name)
                stats["skipped"] += 1
                continue
            n = stats["downloaded"] + stats["errors"] + 1
            if dry_run:
                logger.info("[dry-run] Would download (%d/%d): %s", n, total, name)
                stats["downloaded"] += 1
                continue
            try:
                logger.info("Downloading (%d/%d): %s...", n, total, name)
                self.download_file(entry, local_dir)
                stats["downloaded"] += 1
            except Exception as e:
                logger.error("ERROR downloading %s: %s", name, e)
                stats["errors"] += 1

        return stats

    @staticmethod
    def _is_valid_windows_name(name: str) -> bool:
        """Return False if name contains characters or patterns forbidden on Windows."""
        import re
        if re.search(r'[\\/:*?"<>|]', name):
            return False
        if re.match(r'^(CON|PRN|AUX|NUL|COM\d|LPT\d)(\.|$)', name, re.IGNORECASE):
            return False
        if name.endswith((" ", ".")):
            return False
        return True

    @staticmethod
    def _file_matches(
        name: str,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> bool:
        """Check if a filename matches include/exclude filters.

        If include is set, the file must match at least one pattern.
        If exclude is set, the file must not match any pattern.
        Exclude takes precedence over include.
        """
        if exclude and any(fnmatch.fnmatch(name, p) for p in exclude):
            return False
        if include and not any(fnmatch.fnmatch(name, p) for p in include):
            return False
        return True
    def sync_directory(
        self,
        local_dir: Path,
        remote_path: str = "/",
        *,
        dry_run: bool = False,
        recursive: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> Counter:
        """Synchronize a local directory to Shaper Hub.

        Returns a Counter with keys: uploaded, updated, skipped, errors.
        """
        stats: Counter = Counter()
        remote_path = remote_path.rstrip("/") + "/" if remote_path != "/" else "/"

        if not dry_run:
            self.ensure_remote_path(remote_path)
            remote_files = self.get_remote_files(remote_path)
        else:
            remote_files = {}

        for entry in sorted(local_dir.iterdir()):
            # Skip hidden files/directories
            if entry.name.startswith("."):
                continue

            if entry.is_dir() and recursive:
                logger.info("Directory: %s/", entry.name)
                stats += self.sync_directory(
                    entry,
                    f"{remote_path}{entry.name}/",
                    dry_run=dry_run,
                    recursive=True,
                    include=include,
                    exclude=exclude,
                )
                continue

            if not entry.is_file():
                continue

            if not self._file_matches(entry.name, include, exclude):
                logger.debug("Filtered out: %s", entry.name)
                continue

            local_mtime = datetime.fromtimestamp(entry.stat().st_mtime, tz=timezone.utc)

            if entry.name in remote_files:
                if local_mtime <= remote_files[entry.name]:
                    logger.info("Skipped (up to date): %s", entry.name)
                    stats["skipped"] += 1
                    continue
                action = "updated"
            else:
                action = "uploaded"

            if dry_run:
                logger.info(
                    "[dry-run] Would %s: %s",
                    "update" if action == "updated" else "upload",
                    entry.name,
                )
                stats[action] += 1
                continue

            try:
                logger.info(
                    "%s: %s...",
                    "Updating" if action == "updated" else "Uploading",
                    entry.name,
                )
                if action == "updated":
                    self.delete_file(remote_path, entry.name)
                blob_id = self._upload_file(remote_path, entry)
                logger.info("OK: %s (blob: %s)", entry.name, blob_id)
                stats[action] += 1
            except Exception as e:
                logger.error("ERROR for %s: %s", entry.name, e)
                stats["errors"] += 1

        return stats


    def watch_directory(
        self,
        local_dir: Path,
        remote_path: str = "/",
        *,
        recursive: bool = True,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ) -> None:
        """Watch a local directory for changes and sync them to Shaper Hub.

        Performs an initial full sync, then uses inotify to watch for file
        creation and modification events. Runs until interrupted with Ctrl+C.
        """
        # Initial sync
        logger.info("Initial sync...")
        stats = self.sync_directory(
            local_dir, remote_path, recursive=recursive,
            include=include, exclude=exclude,
        )
        logger.info(
            "Initial sync done: %d uploaded, %d updated, %d skipped, %d error(s).",
            stats["uploaded"], stats["updated"], stats["skipped"], stats["errors"],
        )

        observer = Observer()
        event_handler = sync_files(self, local_dir, remote_path, include=include, exclude=exclude)
        if recursive:
            observer.schedule(event_handler, local_dir, recursive=True)
        else:
            observer.schedule(event_handler, local_dir)
        observer.start()
        logger.info("Watching for changes... (Ctrl+C to stop)")

        try:
            while True:
                t.sleep(1) # Keep the main thread alive
        except KeyboardInterrupt:
            observer.stop()
        observer.join()

class sync_files(FileSystemEventHandler):
    # Seconds to wait after the last event before uploading, to avoid
    # reading a file that is still being written.
    DEBOUNCE = 0.5

    def __init__(
        self,
        client: ShaperHubClient,
        local_dir: Path,
        remote_path: str,
        include: list[str] | None = None,
        exclude: list[str] | None = None,
    ):
        self.client = client
        self.local_dir = local_dir
        self.remote_path = remote_path
        self.include = include
        self.exclude = exclude
        self._pending: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def on_any_event(self, event):
        if event.is_directory:
            return

        # Map watchdog event types to the file path that should be uploaded.
        # "moved" is the atomic rename pattern editors use for safe saves
        # (equivalent to inotify's IN_MOVED_TO).
        if event.event_type in ("created", "modified"):
            full_path = Path(event.src_path)
        elif event.event_type == "moved":
            full_path = Path(event.dest_path)
        else:
            return

        # Skip hidden files
        if full_path.name.startswith("."):
            return

        logger.debug("Event %s: %s", event.event_type, full_path)
        self._schedule(full_path)

    def _schedule(self, full_path: Path) -> None:
        """Debounce uploads: reset the timer each time the file changes."""
        key = str(full_path)
        with self._lock:
            existing = self._pending.pop(key, None)
            if existing:
                existing.cancel()
            timer = threading.Timer(self.DEBOUNCE, self._upload, args=(full_path,))
            self._pending[key] = timer
            timer.start()

    def _upload(self, full_path: Path) -> None:
        with self._lock:
            self._pending.pop(str(full_path), None)

        if not full_path.is_file():
            return

        rel = full_path.parent.relative_to(self.local_dir)
        rpath = self.remote_path.rstrip("/") + "/" if self.remote_path != "/" else "/"
        if str(rel) != ".":
            rpath += str(rel) + "/"

        try:
            self.client.sync_file(full_path, rpath)
        except Exception as e:
            logger.error("ERROR for %s: %s", full_path.name, e)
            return

        stats = self.client.download_directory(
            self.local_dir, self.remote_path,
            recursive=True,
            include=self.include,
            exclude=self.exclude,
        )
        if stats["downloaded"]:
            logger.info("Downloaded %d new file(s).", stats["downloaded"])

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize a local directory to Shaper Hub.",
    )
    parser.add_argument("directory", type=Path, help="Local directory to synchronize.")
    parser.add_argument(
        "--email",
        default=environ.get("SHAPER_EMAIL"),
        required="SHAPER_EMAIL" not in environ,
        help="Shaper account email (default: $SHAPER_EMAIL).",
    )
    parser.add_argument(
        "--password",
        default=environ.get("SHAPER_PASSWORD"),
        required="SHAPER_PASSWORD" not in environ,
        help="Shaper account password (default: $SHAPER_PASSWORD).",
    )
    parser.add_argument(
        "--remote-path", default="/", help="Remote destination path (default: /)."
    )
    parser.add_argument(
        "--download", action="store_true",
        help="Download files from Shaper Hub that are not present locally.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate without uploading/downloading anything."
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="Do not synchronize subdirectories."
    )
    parser.add_argument(
        "--watch", "-w", action="store_true",
        help="Watch directory for changes and sync continuously.",
    )
    parser.add_argument(
        "--include", action="append", default=["*.svg"],
        help="Only sync files matching this pattern (default: *.svg). Can be repeated.",
    )
    parser.add_argument(
        "--exclude", action="append", default=None,
        help="Exclude files matching this pattern. Can be repeated.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show more details."
    )

    args = parser.parse_args()
    logging.basicConfig(
        format="%(message)s",
        level=logging.DEBUG if args.verbose else logging.INFO,
    )

    if not args.remote_path.startswith("/"):
        args.remote_path = "/" + args.remote_path

    if not args.directory.is_dir():
        logger.error("%s is not a directory.", args.directory)
        sys.exit(1)

    if args.dry_run:
        logger.info("[Dry-run mode enabled -- no changes will be made]")

    client = ShaperHubClient(args.email, args.password)

    if args.watch:
        client.watch_directory(
            args.directory,
            args.remote_path,
            recursive=not args.no_recursive,
            include=args.include,
            exclude=args.exclude,
        )
    elif args.download:
        stats = client.download_directory(
            args.directory,
            args.remote_path,
            dry_run=args.dry_run,
            recursive=not args.no_recursive,
            include=args.include,
            exclude=args.exclude,
        )
        logger.info("")
        logger.info(
            "Done: %d downloaded, %d skipped, %d error(s).",
            stats["downloaded"],
            stats["skipped"],
            stats["errors"],
        )
    else:
        stats = client.sync_directory(
            args.directory,
            args.remote_path,
            dry_run=args.dry_run,
            recursive=not args.no_recursive,
            include=args.include,
            exclude=args.exclude,
        )
        logger.info("")
        logger.info(
            "Done: %d uploaded, %d updated, %d skipped, %d error(s).",
            stats["uploaded"],
            stats["updated"],
            stats["skipped"],
            stats["errors"],
        )


if __name__ == "__main__":
    main()
