"""
GiisLostFound — storage.py

All GitHub persistence lives here. app.py calls these functions
and knows nothing about GitHub API details, base64, or SHAs.

Render Environment Variables required:
  GITHUB_TOKEN   — Fine-grained PAT with Contents: Read & Write
  GITHUB_REPO    — e.g. "revasindhu/GiisLostFound"
  GITHUB_BRANCH  — e.g. "main" (default)

Local development:
  When GITHUB_TOKEN / GITHUB_REPO are absent, read/write
  the local data.json file so the site works without credentials.
"""

import os
import json
import base64
import uuid
import io
import requests
from PIL import Image

# =============================================================================
#  CONFIGURATION
# =============================================================================

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO    = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH  = os.getenv("GITHUB_BRANCH", "main")
DATA_PATH      = "data.json"
UPLOADS_PREFIX = "static/uploads"

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_UPLOAD_BYTES   = 10 * 1024 * 1024  # 10 MB

_API = "https://api.github.com"
_RAW = "https://raw.githubusercontent.com"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_DATA = os.path.join(BASE_DIR, "data.json")
_LOCAL_UPLOADS = os.path.join(BASE_DIR, "static", "uploads")


# =============================================================================
#  LOW-LEVEL GITHUB HELPERS
# =============================================================================

def _headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def github_get_file(repo_path):
    """
    Fetch a file from GitHub.
    Returns (decoded_bytes, sha). Returns (None, None) if file doesn't exist.
    Raises RuntimeError on other failures.
    """
    url  = f"{_API}/repos/{GITHUB_REPO}/contents/{repo_path}"
    resp = requests.get(url, headers=_headers(), timeout=15)
    if resp.status_code == 404:
        return None, None
    if not resp.ok:
        raise RuntimeError(f"GitHub GET {repo_path} failed ({resp.status_code})")
    data    = resp.json()
    content = base64.b64decode(data["content"].replace("\n", "").replace("\r", ""))
    return content, data.get("sha")


def github_update_file(repo_path, content_bytes, sha, commit_message):
    """
    Create or update a file on GitHub.
    sha=None when creating a new file.
    Returns the new blob sha.
    """
    url  = f"{_API}/repos/{GITHUB_REPO}/contents/{repo_path}"
    body = {
        "message": commit_message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch":  GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(url, headers=_headers(), json=body, timeout=20)
    if not resp.ok:
        raise RuntimeError(f"GitHub PUT {repo_path} failed ({resp.status_code})")
    return resp.json().get("content", {}).get("sha")


def github_upload_image(repo_path, image_bytes, commit_message):
    """Upload a new image (no sha required)."""
    return github_update_file(repo_path, image_bytes, sha=None, commit_message=commit_message)


def github_delete_file(repo_path, sha, commit_message):
    """Delete a file on GitHub."""
    url  = f"{_API}/repos/{GITHUB_REPO}/contents/{repo_path}"
    body = {"message": commit_message, "sha": sha, "branch": GITHUB_BRANCH}
    resp = requests.delete(url, headers=_headers(), json=body, timeout=15)
    if not resp.ok:
        raise RuntimeError(f"GitHub DELETE {repo_path} failed ({resp.status_code})")


# =============================================================================
#  LOCAL / GITHUB SWITCHING
# =============================================================================

def _github_configured():
    """True when both required GitHub env vars are set (i.e. on Render)."""
    return bool(GITHUB_TOKEN and GITHUB_REPO)


def _local_read():
    """Read data.json from local filesystem."""
    if not os.path.exists(_LOCAL_DATA):
        return []
    with open(_LOCAL_DATA, "r", encoding="utf-8") as f:
        return json.load(f)


def _local_write(items):
    """Write items list to local data.json."""
    with open(_LOCAL_DATA, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2)


# =============================================================================
#  NORMALIZATION
# =============================================================================

def _normalize(items):
    """
    Backward-compat: ensure every item has the fields introduced
    in this version. Old records without status/claims work transparently.
    """
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item.setdefault("status", "available")
        item.setdefault("claims", [])
        item.setdefault("type", "lost")
        for claim in item["claims"]:
            if isinstance(claim, dict) and "id" not in claim:
                seed = f"{claim.get('claimer')}-{claim.get('date')}-{claim.get('message','')[:20]}"
                claim["id"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
        result.append(item)
    return result


# =============================================================================
#  READ
# =============================================================================

def get_items():
    """
    Load and return all items (all statuses).
    Uses GitHub on Render, local file locally.
    """
    if not _github_configured():
        return _normalize(_local_read())
    content, _ = github_get_file(DATA_PATH)
    if content is None:
        return []
    return _normalize(json.loads(content.decode("utf-8")))


def save_items(items):
    """
    Overwrite data.json with items list.
    Used only for simple local writes; prefer _atomic_update for GitHub.
    """
    if not _github_configured():
        _local_write(items)
        return
    # For GitHub we always use _atomic_update to handle SHA conflicts.
    raise RuntimeError("Use _atomic_update for GitHub writes.")


# =============================================================================
#  ATOMIC GITHUB UPDATE
# =============================================================================

def _atomic_update(modifier_fn, commit_message, max_retries=5):
    """
    The core read-modify-write loop for GitHub.
    1. GET latest data.json to obtain current sha.
    2. Apply modifier_fn(items) → modified_items.
    3. PUT back. If 409 (SHA conflict), retry from step 1.
    modifier_fn may raise to abort (LookupError, PermissionError, etc.).
    """
    url = f"{_API}/repos/{GITHUB_REPO}/contents/{DATA_PATH}"

    for attempt in range(max_retries):
        resp = requests.get(url, headers=_headers(), timeout=15)

        sha, items = None, []
        if resp.status_code == 200:
            data    = resp.json()
            sha     = data.get("sha")
            content = base64.b64decode(data["content"].replace("\n", "").replace("\r", ""))
            items   = json.loads(content.decode("utf-8"))
        elif resp.status_code == 404:
            sha, items = None, []
        else:
            raise RuntimeError(f"GitHub GET failed ({resp.status_code})")

        items    = _normalize(items)
        modified = modifier_fn(items)   # may raise to abort

        encoded  = base64.b64encode(json.dumps(modified, indent=2).encode()).decode()
        body     = {"message": commit_message, "content": encoded, "branch": GITHUB_BRANCH}
        if sha:
            body["sha"] = sha

        put = requests.put(url, headers=_headers(), json=body, timeout=20)
        if put.status_code == 409:
            print(f"[storage] SHA conflict, retry {attempt + 1}/{max_retries}")
            continue
        if not put.ok:
            raise RuntimeError(f"GitHub PUT failed ({put.status_code})")
        return modified

    raise RuntimeError("Failed after max retries — persistent SHA conflict.")


def _local_modify(modifier_fn):
    """For local dev: read → apply modifier → write back."""
    items    = _normalize(_local_read())
    modified = modifier_fn(items)  # may raise to abort
    _local_write(modified)
    return modified


def _update(modifier_fn, commit_message):
    """Dispatch to GitHub or local depending on configuration."""
    if _github_configured():
        return _atomic_update(modifier_fn, commit_message)
    return _local_modify(modifier_fn)


# =============================================================================
#  PUBLIC STORAGE API
# =============================================================================

# ── Reports ──────────────────────────────────────────────────────────────────

def get_pending_reports():
    """Return items with status='pending' (submitted by students, awaiting admin review)."""
    return [i for i in get_items() if i.get("status") == "pending"]


def add_report(report_data, image_file=None):
    """
    Save a new report (pending or available if admin-added).
    Uploads image first; rolls back orphan on failure.
    """
    img_repo_path = None
    img_sha       = None

    if image_file:
        try:
            url_path, img_repo_path, img_sha = upload_image(image_file)
            report_data["image"] = url_path
        except Exception as e:
            raise ValueError(f"Image upload failed: {e}")

    def modifier(items):
        items.insert(0, report_data)
        return items

    try:
        _update(modifier, f"Add report: {report_data['name']}")
    except Exception:
        if img_repo_path and img_sha:
            try:
                github_delete_file(img_repo_path, img_sha, f"Rollback orphan image: {img_repo_path}")
            except Exception as del_err:
                print(f"[storage] Orphan image cleanup failed: {del_err}")
        raise

    return report_data


def approve_report(report_id, edits=None):
    """
    Approve a pending report.
    Sets status='available' → item appears in public Lost Box.
    Optional edits dict can update name/category/description/location.
    Raises LookupError if report_id not found.
    """
    def modifier(items):
        item = next((i for i in items if i.get("id") == report_id), None)
        if item is None:
            raise LookupError(f"Report {report_id} not found.")
        if item.get("status") != "pending":
            raise ValueError("Only pending reports can be approved.")
        item["status"] = "available"
        if edits:
            for k, v in edits.items():
                if v:
                    item[k] = v
        return items

    _update(modifier, f"Approve report: {report_id}")


def reject_report(report_id):
    """
    Reject a pending report.
    Sets status='rejected' — hidden from all public views.
    Raises LookupError if not found.
    """
    def modifier(items):
        item = next((i for i in items if i.get("id") == report_id), None)
        if item is None:
            raise LookupError(f"Report {report_id} not found.")
        item["status"] = "rejected"
        return items

    _update(modifier, f"Reject report: {report_id}")


# ── Claims ────────────────────────────────────────────────────────────────────

def add_claim(item_id, claim_data):
    """
    Append a claim to an available Lost Box item.
    Raises LookupError if item not found.
    Raises PermissionError if item is not available (returned or pending).
    """
    def modifier(items):
        item = next((i for i in items if i.get("id") == item_id), None)
        if item is None:
            raise LookupError("Item not found.")
        if item.get("status") != "available":
            raise PermissionError("Claims can only be submitted for available Lost Box items.")
        item.setdefault("claims", []).append(claim_data)
        return items

    _update(modifier, f"Add claim to item: {item_id}")
    return claim_data


# ── Return ────────────────────────────────────────────────────────────────────

def mark_returned(item_id, returned_to, returned_at):
    """
    Mark an item as returned.
    Sets status='returned', records returned_to and returned_at.
    Claims are never deleted.
    Raises LookupError if item not found.
    """
    def modifier(items):
        item = next((i for i in items if i.get("id") == item_id), None)
        if item is None:
            raise LookupError("Item not found.")
        item["status"]      = "returned"
        item["returned_to"] = returned_to
        item["returned_at"] = returned_at
        return items

    _update(modifier, f"Mark returned: {item_id} → {returned_to or 'owner'}")
    return True


# ── Delete ────────────────────────────────────────────────────────────────────

def delete_item(item_id):
    """
    Permanently remove an item from data.json.
    Raises LookupError if item not found.
    """
    def modifier(items):
        before = len(items)
        result = [i for i in items if i.get("id") != item_id]
        if len(result) == before:
            raise LookupError("Item not found.")
        return result

    _update(modifier, f"Delete item: {item_id}")


# =============================================================================
#  IMAGE HELPERS
# =============================================================================

def _validate_image(file):
    """
    Validate extension, size, and image content.
    Returns (ext, raw_bytes). Raises ValueError on any failure.
    """
    if not file or not file.filename:
        raise ValueError("No file provided.")

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported format. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}.")

    raw = file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("File exceeds the 10 MB size limit.")

    try:
        img = Image.open(io.BytesIO(raw))
        img.verify()
    except Exception:
        raise ValueError("The file does not appear to be a valid image.")

    return ext, raw


def upload_image(file):
    """
    Validate and upload image to GitHub (or save locally for dev).
    Returns (url_path, repo_path_or_local_path, sha_or_None).
    """
    ext, raw  = _validate_image(file)
    filename  = f"{uuid.uuid4().hex}.{ext}"

    if not _github_configured():
        # Local dev: save to static/uploads/
        os.makedirs(_LOCAL_UPLOADS, exist_ok=True)
        filepath = os.path.join(_LOCAL_UPLOADS, filename)
        with open(filepath, "wb") as fh:
            fh.write(raw)
        return f"/uploads/{filename}", filepath, None

    repo_path = f"{UPLOADS_PREFIX}/{filename}"
    sha       = github_upload_image(repo_path, raw, f"Upload image: {filename}")
    return f"/uploads/{filename}", repo_path, sha


def delete_image(filename, sha):
    """Delete a named image from GitHub. sha required."""
    repo_path = f"{UPLOADS_PREFIX}/{filename}"
    github_delete_file(repo_path, sha, f"Delete image: {filename}")


def fetch_image_bytes(filename):
    """
    Fetch raw image bytes from GitHub raw CDN for on-demand caching.
    Returns bytes or None if not found.
    """
    url = f"{_RAW}/{GITHUB_REPO}/{GITHUB_BRANCH}/{UPLOADS_PREFIX}/{filename}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        print(f"[storage] fetch_image_bytes({filename}): {e}")
    return None
