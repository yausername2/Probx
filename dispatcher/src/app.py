from flask import Flask, render_template, request, jsonify
from dispatcher import Dispatcher, get_mongo

app = Flask(__name__)

# Create a shared Dispatcher instance (no need for live mode in web UI)
mongo_client, mongo_db, crawler_collection = get_mongo()
dispatcher = Dispatcher(mongo_client=mongo_client, mongo_db=mongo_db, crawler_collection=crawler_collection, mode="web")  # Custom mode, no background thread

# --- Routes ---
@app.route('/')
def index():
    """
    Display the current handlers.
    """
    # Convert regex patterns to strings for JSON serialization
    handlers = {
        name: {
            "regex": handler["regex"].pattern,  # Convert regex to string
            "queue": handler["queue"],
            "collection": handler["collection"]
        }
        for name, handler in dispatcher.handlers.items()
    }
    return render_template("index.html", handlers=handlers)

@app.route('/add_handler', methods=['POST'])
def add_new_handler():
    """
    Add a new handler from the form submission.
    """
    name = request.form.get('name')
    regex = request.form.get('regex')
    queue = request.form.get('queue')
    collection = request.form.get('collection')

    if name and regex and queue and collection:
        handler = dispatcher.add_handler(name, regex, queue, collection)
        if handler:
            return jsonify({'status': 'success', 'message': 'Handler added successfully.', 'handler': handler['name']}), 201
        return jsonify({'status': 'error', 'message': 'Failed to add handler.'}), 500
    return jsonify({'status': 'error', 'message': 'All fields are required.'}), 400

@app.route('/trigger_backfill', methods=['POST'])
def trigger_backfill_process():
    """
    Trigger the backfill process.
    """
    try:
        result = dispatcher.trigger_backfill()
        return jsonify({'status': 'success', 'message': result}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/reload_handlers', methods=['POST'])
def reload_handlers_process():
    """
    Reload the handlers dynamically.
    """
    try:
        count = dispatcher.reload_handlers()
        return jsonify({'status': 'success', 'message': f'{count} handlers reloaded.'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5001)
