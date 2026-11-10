import os

from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Loads DATABRICKS_* (and RECON_BACKEND, if you set it there instead of
# exporting it) from a .env file next to this one, if present. Does nothing
# if there's no .env -- existing exported environment variables still work.
load_dotenv()

# One app, two interchangeable backends behind the same functions:
#   RECON_BACKEND unset or "sqlite"     -> backend/sqlite_backend.py  (local, zero-config, as before)
#   RECON_BACKEND=databricks            -> backend/databricks_backend.py (Unity Catalog + a Databricks job)
# See databricks_backend.py's docstring for the DATABRICKS_* environment variables it needs.
BACKEND_NAME = os.environ.get('RECON_BACKEND', 'sqlite').lower()
if BACKEND_NAME == 'databricks':
    import databricks_backend as backend
else:
    import sqlite_backend as backend

app = Flask(__name__, template_folder='frontend', static_folder='frontend/static')
CORS(app)

# The public deployment runs with RECON_BACKEND=databricks and no login wall --
# anyone with the URL can trigger uploads and a real Databricks job run. These
# per-IP limits don't stop a determined abuser, but they stop a stray bot or an
# accidental refresh-loop from burning Databricks compute or filling the
# Unity Catalog volume. In-memory storage is fine for a single free-tier
# instance; set RATELIMIT_STORAGE_URI (e.g. a Redis URL) if this ever runs
# behind more than one worker/process.
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=['200 per day', '50 per hour'],
    storage_uri=os.environ.get('RATELIMIT_STORAGE_URI', 'memory://'),
)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/backend', methods=['GET'])
def which_backend():
    """So the frontend (or you, in a browser tab) can tell which backend is live."""
    return jsonify({'backend': BACKEND_NAME})


@app.route('/api/upload-gl', methods=['POST'])
@limiter.limit('20 per hour')
def upload_gl():
    try:
        return jsonify(backend.upload_gl(request.files['file']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/upload-bank', methods=['POST'])
@limiter.limit('20 per hour')
def upload_bank():
    try:
        return jsonify(backend.upload_bank(request.files['file']))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/reconcile', methods=['POST'])
@limiter.limit('10 per hour')  # this is the one that spends real Databricks compute
def reconcile():
    try:
        data = request.json or {}
        tolerance = float(data.get('tolerance', 0.01))
        return jsonify(backend.run_reconciliation(tolerance))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/summary', methods=['GET'])
def get_summary():
    try:
        return jsonify(backend.get_summary())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/matches', methods=['GET'])
def get_matches():
    try:
        return jsonify(backend.get_matches())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/exceptions', methods=['GET'])
def get_exceptions():
    try:
        return jsonify(backend.get_exceptions())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/exceptions/<exc_id>/resolve', methods=['POST'])
def resolve_exception(exc_id):
    try:
        return jsonify(backend.resolve_exception(exc_id))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export-report', methods=['GET'])
def export_report():
    try:
        from datetime import datetime
        output = backend.export_report()
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f'Reconciliation_Report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.xlsx',
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print(f"Starting with RECON_BACKEND={BACKEND_NAME}")
    app.run(debug=True, port=5000)
