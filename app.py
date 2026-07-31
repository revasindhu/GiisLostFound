"""
GiisLostFound — app.py
Flask application for GIIS Lost & Found Hub.
All storage is via GitHub (storage.py). No local file mode.
"""

from flask import Flask, render_template, request, jsonify, send_from_directory, session, redirect, url_for
from functools import wraps
import os
import uuid
import secrets
from datetime import datetime

import storage

app = Flask(__name__)

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "password")
_secret = os.getenv("SECRET_KEY")
app.secret_key = _secret if _secret else secrets.token_hex(32)

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# =============================================================================
#  AUTH DECORATORS
# =============================================================================

def admin_required(f):
    """Redirect to login for page routes if admin session missing."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


def api_admin_required(f):
    """Return 401 JSON for API routes if admin session missing."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Unauthorized."}), 401
        return f(*args, **kwargs)
    return decorated


def csrf_required(f):
    """Validate X-CSRF-Token header against session token."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("X-CSRF-Token", "")
        if not token or token != session.get("csrf_token"):
            return jsonify({"error": "Invalid or missing CSRF token."}), 403
        return f(*args, **kwargs)
    return decorated


# =============================================================================
#  PUBLIC ROUTES
# =============================================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """
    Serve uploaded images.
    1. Serve from local disk cache if present.
    2. Otherwise fetch from GitHub raw CDN, cache it, then serve.
    """
    filename = os.path.basename(filename)  # prevent path traversal
    local_path = os.path.join(UPLOAD_FOLDER, filename)

    if os.path.exists(local_path):
        return send_from_directory(UPLOAD_FOLDER, filename)

    image_bytes = storage.fetch_image_bytes(filename)
    if image_bytes is None:
        return "Image not found.", 404

    with open(local_path, "wb") as fh:
        fh.write(image_bytes)
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.route("/api/items", methods=["GET"])
def api_get_items():
    """
    Return ONLY approved Lost Box items (status=available).
    Pending reports and returned items are never shown publicly.
    Supports ?category=, ?q= query params.
    """
    try:
        items = storage.get_items()
    except Exception:
        return jsonify({"error": "Failed to load items."}), 500

    # Public homepage only shows approved available items
    items = [i for i in items if i.get("status") == "available"]

    category = request.args.get("category", "")
    query    = request.args.get("q", "").lower()

    if category:
        items = [i for i in items if i.get("category") == category]
    if query:
        items = [i for i in items if
                 query in i.get("name", "").lower() or
                 query in i.get("description", "").lower() or
                 query in i.get("location", "").lower()]

    return jsonify(items)


@app.route("/api/report", methods=["POST"])
def api_submit_report():
    """
    Students submit a found-item report.
    The report is stored as status='pending' — it is NOT visible publicly.
    Admin must approve it before it appears in the Lost Box.
    """
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Item name is required."}), 400

    report = {
        "id":          str(uuid.uuid4()),
        "type":        "lost",          # all reports become Lost Box items on approval
        "name":        name,
        "category":    request.form.get("category", "Other"),
        "description": request.form.get("description", ""),
        "location":    request.form.get("location", ""),
        "reporter":    request.form.get("reporter", "Anonymous"),
        "date":        datetime.now().strftime("%b %d"),
        "image":       None,
        "status":      "pending",       # hidden from public until admin approves
        "claims":      [],
    }

    file = request.files.get("image")
    has_file = file and file.filename

    try:
        result = storage.add_report(report, file if has_file else None)
        return jsonify({"message": "Report submitted successfully."}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Failed to submit report. Please try again."}), 500


@app.route("/api/stats", methods=["GET"])
def api_stats():
    """
    Public stats endpoint.
    Returns counts of approved items only (available + returned).
    Pending and rejected reports are never counted.
    """
    try:
        all_items = storage.get_items()
    except Exception:
        return jsonify({"total": 0, "available": 0, "returned": 0})

    available = sum(1 for i in all_items if i.get("status") == "available")
    returned  = sum(1 for i in all_items if i.get("status") == "returned")
    return jsonify({
        "total":     available + returned,
        "available": available,
        "returned":  returned,
    })


@app.route("/api/claim", methods=["POST"])
def api_claim_item():
    """
    Submit a claim on an approved Lost Box item.
    Rejected server-side if item is not available.
    """
    data    = request.get_json(silent=True) or {}
    item_id = data.get("item_id", "").strip()
    claimer = data.get("name", "").strip()
    grade   = data.get("grade", "").strip()
    message = data.get("message", "").strip()

    if not item_id:
        return jsonify({"error": "Item ID is required."}), 400
    if not claimer:
        return jsonify({"error": "Your name is required."}), 400
    if not grade:
        return jsonify({"error": "Your class / grade is required."}), 400

    claim = {
        "id":      str(uuid.uuid4()),
        "claimer": claimer,
        "grade":   grade,
        "message": message,
        "date":    datetime.now().strftime("%b %d %Y"),
    }

    try:
        storage.add_claim(item_id, claim)
        return jsonify({"message": "Claim submitted! The administrator will be in touch."})
    except LookupError:
        return jsonify({"error": "Item not found."}), 404
    except PermissionError as e:
        return jsonify({"error": str(e)}), 409
    except Exception:
        return jsonify({"error": "Failed to submit claim. Please try again."}), 500


# =============================================================================
#  ADMIN PAGE ROUTES
# =============================================================================

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    error = None
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            session["csrf_token"] = secrets.token_hex(32)
            session.permanent = True
            return redirect(url_for("admin_dashboard"))
        error = "Invalid username or password."

    return render_template("admin_login.html", error=error)


@app.route("/admin")
@admin_required
def admin_dashboard():
    return render_template("admin_dashboard.html", csrf_token=session.get("csrf_token"))


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# =============================================================================
#  ADMIN API — READ
# =============================================================================

@app.route("/api/admin/items", methods=["GET"])
@api_admin_required
def api_admin_get_items():
    """Return ALL items (all statuses) for admin dashboard."""
    try:
        return jsonify(storage.get_items())
    except Exception:
        return jsonify({"error": "Failed to load items."}), 500


# =============================================================================
#  ADMIN API — REPORT ACTIONS
# =============================================================================

@app.route("/api/admin/reports/<report_id>/approve", methods=["POST"])
@api_admin_required
@csrf_required
def api_approve_report(report_id):
    """Approve a pending report — item becomes available in the Lost Box."""
    # Admin may optionally edit fields before approval
    data = request.get_json(silent=True) or {}
    edits = {
        "name":        data.get("name", "").strip(),
        "category":    data.get("category", "").strip(),
        "description": data.get("description", "").strip(),
        "location":    data.get("location", "").strip(),
    }
    # Remove empty edits so we don't overwrite with blanks
    edits = {k: v for k, v in edits.items() if v}

    try:
        storage.approve_report(report_id, edits)
        return jsonify({"message": "Report approved. Item is now in the Lost Box."})
    except LookupError:
        return jsonify({"error": "Report not found."}), 404
    except Exception:
        return jsonify({"error": "Failed to approve report."}), 500


@app.route("/api/admin/reports/<report_id>/reject", methods=["POST"])
@api_admin_required
@csrf_required
def api_reject_report(report_id):
    """Reject a pending report — it is hidden from all views."""
    try:
        storage.reject_report(report_id)
        return jsonify({"message": "Report rejected."})
    except LookupError:
        return jsonify({"error": "Report not found."}), 404
    except Exception:
        return jsonify({"error": "Failed to reject report."}), 500


# =============================================================================
#  ADMIN API — LOST BOX MANAGEMENT
# =============================================================================

@app.route("/api/admin/items", methods=["POST"])
@api_admin_required
@csrf_required
def api_admin_add_item():
    """Admin manually adds an item directly to the Lost Box (status=available)."""
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "Item name is required."}), 400

    item = {
        "id":          str(uuid.uuid4()),
        "type":        "lost",
        "name":        name,
        "category":    request.form.get("category", "Other"),
        "description": request.form.get("description", ""),
        "location":    request.form.get("location", ""),
        "reporter":    request.form.get("reporter", "Admin"),
        "date":        datetime.now().strftime("%b %d"),
        "image":       None,
        "status":      "available",     # immediately visible — admin bypass
        "claims":      [],
    }

    file = request.files.get("image")
    has_file = file and file.filename

    try:
        result = storage.add_report(item, file if has_file else None)
        return jsonify(result), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        return jsonify({"error": "Failed to add item."}), 500


@app.route("/api/admin/items/<item_id>/return", methods=["POST"])
@api_admin_required
@csrf_required
def api_mark_returned(item_id):
    """Mark an available item as returned to a specific claimant."""
    data        = request.get_json(silent=True) or {}
    returned_to = data.get("returned_to", "").strip()
    returned_at = datetime.now().strftime("%b %d %Y")

    try:
        storage.mark_returned(item_id, returned_to, returned_at)
        return jsonify({"message": f"Item marked as returned to {returned_to or 'owner'}."})
    except LookupError:
        return jsonify({"error": "Item not found."}), 404
    except Exception:
        return jsonify({"error": "Failed to update item."}), 500


@app.route("/api/admin/items/<item_id>", methods=["DELETE"])
@api_admin_required
@csrf_required
def api_delete_item(item_id):
    """Permanently delete an item (any status) from data.json."""
    try:
        storage.delete_item(item_id)
        return jsonify({"message": "Item deleted."})
    except LookupError:
        return jsonify({"error": "Item not found."}), 404
    except Exception:
        return jsonify({"error": "Failed to delete item."}), 500


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
