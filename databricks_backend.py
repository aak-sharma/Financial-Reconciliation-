"""Databricks-backed implementation of the same interface as sqlite_backend.py.
Reads/writes go to Unity Catalog tables and a landing Volume, and reconciliation
runs as the databricks/ job (02_bronze_ingest -> 03_silver_clean -> 04_gold_reconcile)
instead of in-process Pandas.

Required environment variables:
  DATABRICKS_HOST       e.g. https://<workspace>.cloud.databricks.com
  DATABRICKS_TOKEN      a personal access token (Settings -> Developer -> Access tokens)
  DATABRICKS_HTTP_PATH  the SQL warehouse's "HTTP path" (SQL Warehouses -> your warehouse -> Connection details)
  DATABRICKS_JOB_ID     the job id for the bronze/silver/gold pipeline (Workflows -> Jobs -> your job -> URL)
Optional:
  DATABRICKS_CATALOG    default "workspace"
  DATABRICKS_SCHEMA     default "recon" (must match what 01_generate_data.py was run with)

Note on latency: run_reconciliation() blocks until the Databricks job finishes
(serverless compute startup + the job itself), which is seconds-to-minutes rather
than the sub-second response of the SQLite engine. The frontend doesn't need any
change for this -- the "Run Reconciliation" button's spinner will just show for
longer -- but it's worth knowing before you sit through your first run.
"""
import io
import os
import time
from datetime import timedelta

import pandas as pd
from databricks import sql as dbsql
from databricks.sdk import WorkspaceClient

CATALOG = os.environ.get('DATABRICKS_CATALOG', 'workspace')
SCHEMA = os.environ.get('DATABRICKS_SCHEMA', 'recon')
JOB_ID = os.environ.get('DATABRICKS_JOB_ID')
FQ = f'{CATALOG}.{SCHEMA}'

REQUIRED_GL_COLS = ['date', 'account_code', 'description', 'debit', 'credit', 'reference']
REQUIRED_BANK_COLS = ['date', 'amount', 'description', 'reference']


def _workspace_client():
    # Reads DATABRICKS_HOST / DATABRICKS_TOKEN from the environment itself.
    return WorkspaceClient()


def _connect():
    host = os.environ['DATABRICKS_HOST'].replace('https://', '').replace('http://', '').rstrip('/')
    return dbsql.connect(
        server_hostname=host,
        http_path=os.environ['DATABRICKS_HTTP_PATH'],
        access_token=os.environ['DATABRICKS_TOKEN'],
    )


def _query(sql_text, params=None):
    """Runs a SELECT, returns rows as a list of dicts."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql_text, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _execute(sql_text, params=None):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql_text, params or [])


def _upload_csv(file_storage, source, required_cols):
    """Validates columns up front (same check the old app made), then uploads
    the untouched file to the landing volume for Auto Loader to pick up."""
    df = pd.read_csv(file_storage)
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError('Missing required columns: ' + ', '.join(missing))

    file_storage.stream.seek(0)  # rewind past the read above; upload the original bytes
    batch = f'upload_{int(time.time())}'
    path = f'/Volumes/{CATALOG}/{SCHEMA}/landing/{source}/{batch}/data.csv'
    _workspace_client().files.upload(path, file_storage.stream, overwrite=True)
    return len(df)


def upload_gl(file_storage):
    count = _upload_csv(file_storage, 'gl', REQUIRED_GL_COLS)
    return {'success': True, 'message': f'Uploaded {count} GL entries to the Databricks landing volume', 'count': count}


def upload_bank(file_storage):
    count = _upload_csv(file_storage, 'bank', REQUIRED_BANK_COLS)
    return {'success': True, 'message': f'Uploaded {count} bank entries to the Databricks landing volume', 'count': count}


def run_reconciliation(tolerance):
    """Triggers the bronze -> silver -> gold job and waits for it to finish."""
    if not JOB_ID:
        raise RuntimeError('DATABRICKS_JOB_ID is not set')

    run = _workspace_client().jobs.run_now(job_id=int(JOB_ID), job_parameters={'tolerance': str(tolerance)})
    run.result(timeout=timedelta(minutes=30))  # raises if the job fails

    run_row = _query(f'SELECT * FROM {FQ}.gold_recon_runs ORDER BY run_at DESC LIMIT 1')[0]
    by_type = {r['entry_type']: r['n'] for r in _query(f'''
        SELECT entry_type, COUNT(*) AS n FROM {FQ}.gold_exceptions
        WHERE status = 'Open' GROUP BY entry_type
    ''')}
    return {
        'success': True,
        'matched': run_row['matched_pairs'],
        'unmatched_gl': by_type.get('GL', 0),
        'unmatched_bank': by_type.get('Bank', 0),
        'match_rate': run_row['gl_match_rate_pct'],
    }


def get_summary():
    gl_count = _query(f'SELECT COUNT(*) AS n FROM {FQ}.silver_gl')[0]['n']
    bank_count = _query(f'SELECT COUNT(*) AS n FROM {FQ}.silver_bank')[0]['n']
    matched_count = _query(f'SELECT COUNT(*) AS n FROM {FQ}.gold_matches')[0]['n']
    exc = _query(f'''
        SELECT COUNT(*) AS n, COALESCE(SUM(ABS(amount)), 0) AS total
        FROM {FQ}.gold_exceptions WHERE status = 'Open'
    ''')[0]
    return {
        'gl_entries': gl_count,
        'bank_entries': bank_count,
        'matched_entries': matched_count,
        'open_exceptions': exc['n'],
        'exception_amount': round(float(exc['total']), 2),
    }


def get_matches():
    rows = _query(f'''
        SELECT entry_id, bank_reference, account_code, match_type, confidence,
               gl_date, bank_date, gl_amount, bank_amount, variance,
               gl_reference, gl_description, bank_description
        FROM {FQ}.gold_matches ORDER BY confidence DESC LIMIT 100
    ''')
    # Field names matched to what the original frontend expects from /api/matches.
    # credit is always 0 here: gl_amount is already the single signed net amount
    # (debit - credit) computed in silver_gl, there's no separate credit column
    # to report. script.js treats a missing/0 credit as "nothing to subtract".
    for r in rows:
        r['id'] = f"{r['entry_id']}:{r['bank_reference']}"
        r['confidence_score'] = r.pop('confidence')
        r['gl_desc'] = r.pop('gl_description')
        r['bank_desc'] = r.pop('bank_description')
        r['gl_ref'] = r.pop('gl_reference')
        r['debit'] = r.pop('gl_amount')
        r['credit'] = 0
        r['amount'] = r.pop('bank_amount')
    return rows


def get_exceptions():
    rows = _query(f'''
        SELECT id, entry_type, exception_type, amount, description, txn_date, status
        FROM {FQ}.gold_exceptions WHERE status = 'Open' ORDER BY age_days DESC
    ''')
    for r in rows:
        r['entry_id'] = r['id']       # original frontend's field name
        r['created_at'] = r.pop('txn_date')
    return rows


def resolve_exception(exc_id):
    _execute(f"UPDATE {FQ}.gold_exceptions SET status = 'Resolved' WHERE id = ?", [exc_id])
    return {'success': True, 'message': 'Exception resolved'}


def export_report():
    """Export reconciliation report as Excel. Returns a BytesIO positioned at 0."""
    total_gl = _query(f'SELECT COUNT(*) AS n FROM {FQ}.silver_gl')[0]['n']
    total_bank = _query(f'SELECT COUNT(*) AS n FROM {FQ}.silver_bank')[0]['n']
    total_matched = _query(f'SELECT COUNT(*) AS n FROM {FQ}.gold_matches')[0]['n']

    summary_data = {
        'Metric': ['GL Entries', 'Bank Entries', 'Matched', 'Unmatched GL', 'Unmatched Bank', 'Match Rate %'],
        'Count': [total_gl, total_bank, total_matched,
                 total_gl - total_matched, total_bank - total_matched,
                 round((total_matched / max(total_gl, total_bank)) * 100, 2) if max(total_gl, total_bank) else 0],
    }

    matches_df = pd.DataFrame(_query(f'''
        SELECT entry_id, account_code, match_type, confidence, gl_date, gl_amount,
               bank_date, bank_amount FROM {FQ}.gold_matches
    '''))
    exceptions_df = pd.DataFrame(_query(f"SELECT * FROM {FQ}.gold_exceptions WHERE status = 'Open'"))
    unmatched_gl_df = pd.DataFrame(_query(f'''
        SELECT g.* FROM {FQ}.silver_gl g
        WHERE NOT EXISTS (SELECT 1 FROM {FQ}.gold_matches m WHERE m.entry_id = g.entry_id)
    '''))
    unmatched_bank_df = pd.DataFrame(_query(f'''
        SELECT b.* FROM {FQ}.silver_bank b
        WHERE NOT EXISTS (SELECT 1 FROM {FQ}.gold_matches m WHERE m.bank_reference = b.bank_reference)
    '''))

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        pd.DataFrame(summary_data).to_excel(writer, sheet_name='Summary', index=False)
        matches_df.to_excel(writer, sheet_name='Matched Entries', index=False)
        exceptions_df.to_excel(writer, sheet_name='Exceptions', index=False)
        unmatched_gl_df.to_excel(writer, sheet_name='Unmatched GL', index=False)
        unmatched_bank_df.to_excel(writer, sheet_name='Unmatched Bank', index=False)
    output.seek(0)
    return output
