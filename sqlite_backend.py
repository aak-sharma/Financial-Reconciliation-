"""Local backend: the original SQLite + Pandas implementation, refactored into
functions so app.py can swap it for databricks_backend.py behind one interface.
Behavior is unchanged from the original app.py/reconciliation_engine.py, with one
fix: _parse_date no longer silently falls back to "now" for every date (see the
comment in reconciliation_engine.py's _parse_date) -- the date-window checks in
every matching strategy actually run against the transaction date, as intended.

Known limitation carried over from the original: Strategies 1-4 in
reconciliation_engine.py don't guarantee a bank/GL entry is claimed only once
(no mutual-best-match check), so a popular bank row can in principle be matched
to more than one GL entry. The Databricks pipeline (databricks/recon_lib.py)
fixes this with a mutual-best-match resolver; this local engine does not, to
keep it a faithful, low-risk refactor of the original rather than a rewrite.
"""
import io
import sqlite3
from datetime import datetime

import pandas as pd

from reconciliation_engine import ReconciliationEngine

DB_PATH = 'reconciliation.db'
engine = ReconciliationEngine()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS gl_entries (
        id INTEGER PRIMARY KEY,
        date TEXT,
        account_code TEXT,
        description TEXT,
        debit REAL,
        credit REAL,
        reference TEXT,
        uploaded_at TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS bank_entries (
        id INTEGER PRIMARY KEY,
        date TEXT,
        amount REAL,
        description TEXT,
        reference TEXT,
        bank_reference TEXT,
        uploaded_at TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS reconciliation_matches (
        id INTEGER PRIMARY KEY,
        gl_entry_id INTEGER,
        bank_entry_id INTEGER,
        match_type TEXT,
        confidence_score REAL,
        reconciled_at TIMESTAMP,
        FOREIGN KEY(gl_entry_id) REFERENCES gl_entries(id),
        FOREIGN KEY(bank_entry_id) REFERENCES bank_entries(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS exceptions (
        id INTEGER PRIMARY KEY,
        entry_type TEXT,
        entry_id INTEGER,
        exception_type TEXT,
        description TEXT,
        amount REAL,
        created_at TIMESTAMP,
        status TEXT
    )''')

    conn.commit()
    conn.close()


init_db()


def upload_gl(file_storage):
    """Upload GL (General Ledger) data"""
    df = pd.read_csv(file_storage)

    required_cols = ['date', 'account_code', 'description', 'debit', 'credit', 'reference']
    if not all(col in df.columns for col in required_cols):
        raise ValueError('Missing required columns: ' + ', '.join(required_cols))

    df['date'] = pd.to_datetime(df['date'])
    df['debit'] = pd.to_numeric(df['debit'], errors='coerce').fillna(0)
    df['credit'] = pd.to_numeric(df['credit'], errors='coerce').fillna(0)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM gl_entries')

    for _, row in df.iterrows():
        c.execute('''INSERT INTO gl_entries
                    (date, account_code, description, debit, credit, reference, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 (str(row['date']), row['account_code'], row['description'],
                  row['debit'], row['credit'], row['reference'], datetime.now()))

    conn.commit()
    count = len(df)
    conn.close()

    return {'success': True, 'message': f'Uploaded {count} GL entries', 'count': count}


def upload_bank(file_storage):
    """Upload Bank Statement data"""
    df = pd.read_csv(file_storage)

    required_cols = ['date', 'amount', 'description', 'reference']
    if not all(col in df.columns for col in required_cols):
        raise ValueError('Missing required columns: ' + ', '.join(required_cols))

    df['date'] = pd.to_datetime(df['date'])
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce').fillna(0)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('DELETE FROM bank_entries')

    for _, row in df.iterrows():
        bank_ref = row.get('bank_reference', '')
        c.execute('''INSERT INTO bank_entries
                    (date, amount, description, reference, bank_reference, uploaded_at)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                 (str(row['date']), row['amount'], row['description'],
                  row['reference'], bank_ref, datetime.now()))

    conn.commit()
    count = len(df)
    conn.close()

    return {'success': True, 'message': f'Uploaded {count} bank entries', 'count': count}


def run_reconciliation(tolerance):
    """Run reconciliation process"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('SELECT * FROM gl_entries')
    gl_entries = [dict(row) for row in c.fetchall()]

    c.execute('SELECT * FROM bank_entries')
    bank_entries = [dict(row) for row in c.fetchall()]

    conn.close()

    matches, unmatched_gl, unmatched_bank = engine.reconcile(gl_entries, bank_entries, tolerance)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('DELETE FROM reconciliation_matches')
    c.execute('DELETE FROM exceptions')

    for match in matches:
        c.execute('''INSERT INTO reconciliation_matches
                    (gl_entry_id, bank_entry_id, match_type, confidence_score, reconciled_at)
                    VALUES (?, ?, ?, ?, ?)''',
                 (match['gl_id'], match['bank_id'], match['type'],
                  match['confidence'], datetime.now()))

    for gl in unmatched_gl:
        c.execute('''INSERT INTO exceptions
                    (entry_type, entry_id, exception_type, description, amount, created_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 ('GL', gl['id'], 'Unmatched GL Entry',
                  f"GL Entry: {gl['description']}",
                  gl['debit'] - gl['credit'], datetime.now(), 'Open'))

    for bank in unmatched_bank:
        c.execute('''INSERT INTO exceptions
                    (entry_type, entry_id, exception_type, description, amount, created_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                 ('Bank', bank['id'], 'Unmatched Bank Entry',
                  f"Bank Entry: {bank['description']}",
                  bank['amount'], datetime.now(), 'Open'))

    conn.commit()
    conn.close()

    denom = len(matches) + len(unmatched_gl) + len(unmatched_bank)
    return {
        'success': True,
        'matched': len(matches),
        'unmatched_gl': len(unmatched_gl),
        'unmatched_bank': len(unmatched_bank),
        'match_rate': round((len(matches) / denom) * 100, 2) if denom else 0,
    }


def get_summary():
    """Get reconciliation summary"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('SELECT COUNT(*) FROM gl_entries')
    gl_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM bank_entries')
    bank_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM reconciliation_matches')
    matched_count = c.fetchone()[0]

    c.execute('SELECT COUNT(*) FROM exceptions WHERE status = "Open"')
    exception_count = c.fetchone()[0]

    c.execute('SELECT SUM(amount) FROM exceptions WHERE status = "Open"')
    exception_amount = c.fetchone()[0] or 0

    conn.close()

    return {
        'gl_entries': gl_count,
        'bank_entries': bank_count,
        'matched_entries': matched_count,
        'open_exceptions': exception_count,
        'exception_amount': round(exception_amount, 2),
    }


def get_matches():
    """Get all matched entries"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''SELECT
                m.id, m.match_type, m.confidence_score, m.reconciled_at,
                gl.date as gl_date, gl.account_code, gl.description as gl_desc,
                gl.debit, gl.credit, gl.reference as gl_ref,
                b.date as bank_date, b.amount, b.description as bank_desc, b.bank_reference
                FROM reconciliation_matches m
                JOIN gl_entries gl ON m.gl_entry_id = gl.id
                JOIN bank_entries b ON m.bank_entry_id = b.id
                ORDER BY m.reconciled_at DESC
                LIMIT 100''')

    matches = [dict(row) for row in c.fetchall()]
    conn.close()
    return matches


def get_exceptions():
    """Get all open exceptions"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''SELECT * FROM exceptions
                WHERE status = "Open"
                ORDER BY created_at DESC''')

    exceptions = [dict(row) for row in c.fetchall()]
    conn.close()
    return exceptions


def resolve_exception(exc_id):
    """Resolve an exception"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE exceptions SET status = ? WHERE id = ?', ('Resolved', int(exc_id)))
    conn.commit()
    conn.close()
    return {'success': True, 'message': 'Exception resolved'}


def export_report():
    """Export reconciliation report as Excel. Returns a BytesIO positioned at 0."""
    conn = sqlite3.connect(DB_PATH)

    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM gl_entries')
    total_gl = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM bank_entries')
    total_bank = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM reconciliation_matches')
    total_matched = c.fetchone()[0]

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        summary_data = {
            'Metric': ['GL Entries', 'Bank Entries', 'Matched', 'Unmatched GL', 'Unmatched Bank', 'Match Rate %'],
            'Count': [total_gl, total_bank, total_matched,
                     total_gl - total_matched, total_bank - total_matched,
                     round((total_matched / max(total_gl, total_bank)) * 100, 2) if max(total_gl, total_bank) > 0 else 0],
        }
        pd.DataFrame(summary_data).to_excel(writer, sheet_name='Summary', index=False)

        matches_df = pd.read_sql_query('''SELECT
                                        m.id, m.match_type, m.confidence_score, m.reconciled_at,
                                        gl.date as gl_date, gl.account_code, gl.description,
                                        gl.debit, gl.credit,
                                        b.date as bank_date, b.amount
                                        FROM reconciliation_matches m
                                        JOIN gl_entries gl ON m.gl_entry_id = gl.id
                                        JOIN bank_entries b ON m.bank_entry_id = b.id''', conn)
        matches_df.to_excel(writer, sheet_name='Matched Entries', index=False)

        exceptions_df = pd.read_sql_query('''SELECT * FROM exceptions WHERE status = "Open"''', conn)
        exceptions_df.to_excel(writer, sheet_name='Exceptions', index=False)

        unmatched_gl_df = pd.read_sql_query('''SELECT ge.* FROM gl_entries ge
                                               WHERE ge.id NOT IN (SELECT gl_entry_id FROM reconciliation_matches)''', conn)
        unmatched_gl_df.to_excel(writer, sheet_name='Unmatched GL', index=False)

        unmatched_bank_df = pd.read_sql_query('''SELECT be.* FROM bank_entries be
                                                WHERE be.id NOT IN (SELECT bank_entry_id FROM reconciliation_matches)''', conn)
        unmatched_bank_df.to_excel(writer, sheet_name='Unmatched Bank', index=False)

    conn.close()
    output.seek(0)
    return output
