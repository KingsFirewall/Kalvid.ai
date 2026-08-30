"""Delivery storage — Supabase Storage.

Providers hand back a URL that expires (fal and Runware both age their outputs out),
so a finished render is pulled down and re-hosted somewhere durable before it is sent
to a client. Local disk is the fallback; Supabase Storage is used when configured.
"""
from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import httpx

from .config import settings

log = logging.getLogger(__name__)


class StorageError(Exception):
    pass


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.supabase_service_role_key}",
        "apikey": settings.supabase_service_role_key,
    }


def available() -> bool:
    return settings.supabase_configured


def ensure_bucket(public: bool = False) -> str:
    """Create the configured bucket if it does not exist. Private by default —
    finished client work should be reachable only through a signed URL."""
    if not available():
        raise StorageError("Supabase is not configured (check SUPABASE_* in .env)")
    with httpx.Client(timeout=30.0) as c:
        existing = c.get(f"{settings.supabase_url}/storage/v1/bucket", headers=_headers())
        if existing.status_code < 400:
            names = [b.get("name") for b in existing.json()]
            if settings.supabase_bucket in names:
                return "already exists"
        r = c.post(
            f"{settings.supabase_url}/storage/v1/bucket",
            headers={**_headers(), "Content-Type": "application/json"},
            json={"name": settings.supabase_bucket,
                  "id": settings.supabase_bucket,
                  "public": public},
        )
    if r.status_code >= 400:
        raise StorageError(f"could not create bucket: {r.status_code} {r.text[:200]}")
    return "created"


def delete_object(object_path: str) -> None:
    """Remove one stored object (used to clean up preflight test uploads)."""
    if not available():
        return
    with httpx.Client(timeout=30.0) as c:
        c.request(
            "DELETE",
            f"{settings.supabase_url}/storage/v1/object/"
            f"{settings.supabase_bucket}/{object_path}",
            headers=_headers(),
        )


def upload(local_path: Path, object_path: str) -> str:
    """Upload a file to the configured bucket. Returns the storage object path."""
    if not available():
        raise StorageError("Supabase is not configured (check SUPABASE_* in .env)")

    ctype = mimetypes.guess_type(str(local_path))[0] or "application/octet-stream"
    url = (f"{settings.supabase_url}/storage/v1/object/"
           f"{settings.supabase_bucket}/{object_path}")
    with httpx.Client(timeout=300.0) as c:
        r = c.post(
            url,
            headers={**_headers(), "Content-Type": ctype, "x-upsert": "true"},
            content=local_path.read_bytes(),
        )
    if r.status_code >= 400:
        raise StorageError(f"supabase upload {r.status_code}: {r.text[:300]}")
    return object_path


def signed_url(object_path: str, ttl: int | None = None) -> str:
    """Time-limited link for handing a finished render to a client."""
    if not available():
        raise StorageError("Supabase is not configured")
    ttl = ttl or settings.supabase_signed_url_ttl
    url = (f"{settings.supabase_url}/storage/v1/object/sign/"
           f"{settings.supabase_bucket}/{object_path}")
    with httpx.Client(timeout=30.0) as c:
        r = c.post(url, headers=_headers(), json={"expiresIn": ttl})
    if r.status_code >= 400:
        raise StorageError(f"supabase sign {r.status_code}: {r.text[:300]}")
    signed = r.json().get("signedURL") or r.json().get("signedUrl")
    if not signed:
        raise StorageError(f"supabase returned no signed URL: {r.text[:300]}")
    return f"{settings.supabase_url}/storage/v1{signed}"


def archive_output(source_url: str, *, client_name: str, job_id: int,
                   gen_id: int, kind: str = "video") -> str:
    """Pull a provider output down and store it durably.

    Always writes a local copy first (that alone survives the provider expiring the
    URL), then mirrors to Supabase when configured. Returns the best available URL.
    """
    ext = "mp4" if kind == "video" else "png"
    safe_client = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in client_name)
    rel = f"{safe_client}/job-{job_id}/gen-{gen_id}.{ext}"
    local = settings.output_dir / rel
    local.parent.mkdir(parents=True, exist_ok=True)

    try:
        if source_url.startswith(("http://", "https://")):
            with httpx.Client(timeout=300.0, follow_redirects=True) as c:
                r = c.get(source_url)
                r.raise_for_status()
                local.write_bytes(r.content)
        else:
            local.write_bytes(Path(source_url).read_bytes())
    except Exception as exc:
        # Never fail a completed render over archiving — the provider URL still works
        # for now, and the ledger already recorded the real spend.
        log.warning("could not archive job %s gen %s: %s", job_id, gen_id, exc)
        return source_url

    if available():
        try:
            upload(local, rel)
            return signed_url(rel)
        except StorageError as exc:
            log.warning("supabase upload failed for gen %s, keeping local copy: %s",
                        gen_id, exc)
    return str(local)
