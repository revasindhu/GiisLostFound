from flask import Flask, render_template, request, jsonify, send_from_directory
import json
import os
import uuid
from datetime import datetime

app = Flask(__name__)

# Absolute paths — works on Windows and Mac regardless of where you run from
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_FILE     = os.path.join(BASE_DIR, 'data.json')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# ── HELPERS ───────────────────────────────────────────────────────────────────

def load_items():
    """Load items from JSON file. Returns empty list if file doesn't exist."""
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, 'r') as f:
        return json.load(f)

def save_items(items):
    """Save items list to JSON file."""
    with open(DATA_FILE, 'w') as f:
        json.dump(items, f, indent=2)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_image(file):
    """Save uploaded image and return its public URL path."""
    if file and allowed_file(file.filename):
        ext      = file.filename.rsplit('.', 1)[1].lower()
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        # Return a URL path Flask can serve
        return f"/uploads/{filename}"
    return None

# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

# Serve uploaded images from the uploads folder
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

@app.route('/api/items', methods=['GET'])
def get_items():
    items       = load_items()
    filter_type = request.args.get('type', 'all')
    category    = request.args.get('category', '')
    query       = request.args.get('q', '').lower()

    if filter_type != 'all':
        items = [i for i in items if i['type'] == filter_type]
    if category:
        items = [i for i in items if i['category'] == category]
    if query:
        items = [i for i in items if
                 query in i['name'].lower() or
                 query in i['description'].lower() or
                 query in i['location'].lower()]

    return jsonify(items)

@app.route('/api/items', methods=['POST'])
def post_item():
    name = request.form.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Item name is required.'}), 400

    image_url = None
    if 'image' in request.files:
        file = request.files['image']
        if file and file.filename:
            image_url = save_image(file)

    item = {
        'id':          str(uuid.uuid4()),
        'type':        request.form.get('type', 'lost'),
        'name':        name,
        'category':    request.form.get('category', 'Other'),
        'description': request.form.get('description', 'No description provided.'),
        'location':    request.form.get('location', 'Unknown'),
        'reporter':    request.form.get('reporter', 'Anonymous'),
        'date':        datetime.now().strftime('%b %d'),
        'image':       image_url
    }

    items = load_items()
    items.insert(0, item)
    save_items(items)

    return jsonify(item), 201

@app.route('/api/claim', methods=['POST'])
def claim_item():
    data    = request.get_json()
    item_id = data.get('item_id')
    claimer = data.get('name', '').strip()
    grade   = data.get('grade', '')
    message = data.get('message', '')

    if not claimer:
        return jsonify({'error': 'Your name is required.'}), 400

    items = load_items()
    item  = next((i for i in items if i['id'] == item_id), None)
    if not item:
        return jsonify({'error': 'Item not found.'}), 404

    if 'claims' not in item:
        item['claims'] = []
    item['claims'].append({
        'claimer': claimer,
        'grade':   grade,
        'message': message,
        'date':    datetime.now().strftime('%b %d %Y')
    })
    save_items(items)

    return jsonify({'message': f"Claim submitted! {item['reporter']} will be notified."})

# ── RUN ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    print("🔍 Lost & Found server running at http://localhost:5000")
    app.run(debug=True)
