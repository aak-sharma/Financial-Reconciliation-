import pandas as pd
import random
from datetime import datetime, timedelta

# Generate GL Data
def generate_gl_data(num_entries=50):
    """Generate sample GL entries"""

    dates = []
    account_codes = []
    descriptions = []
    debits = []
    credits = []
    references = []

    account_types = {
        '1000': ('Sales Revenue', 5000, 15000),
        '1200': ('Service Income', 2000, 8000),
        '1500': ('Other Income', 500, 3000),
        '2000': ('Expenses', 1000, 5000),
        '2500': ('Utilities', 500, 2000),
        '3000': ('Salaries', 5000, 20000),
    }

    invoice_num = 1000
    check_num = 5000

    for i in range(num_entries):
        date = datetime.now() - timedelta(days=random.randint(0, 30))
        dates.append(date.strftime('%Y-%m-%d'))

        account_code = random.choice(list(account_types.keys()))
        account_codes.append(account_code)

        desc_template, min_amt, max_amt = account_types[account_code]
        descriptions.append(desc_template)

        amount = random.randint(min_amt, max_amt)

        if random.random() < 0.7:
            debits.append(amount)
            credits.append(0)
            references.append(f'INV{invoice_num}')
            invoice_num += 1
        else:
            debits.append(0)
            credits.append(amount)
            references.append(f'CHK{check_num}')
            check_num += 1

    return pd.DataFrame({
        'date': dates, 'account_code': account_codes, 'description': descriptions,
        'debit': debits, 'credit': credits, 'reference': references,
    })


# Generate Bank Data
def generate_bank_data(gl_df, match_rate=0.85):
    """Generate sample bank entries that partially match GL data"""

    bank_entries = []

    for idx, row in gl_df.iterrows():
        if random.random() < match_rate:
            amount = row['debit'] - row['credit']
            if random.random() < 0.1:
                amount = amount * random.uniform(0.99, 1.01)

            date = pd.to_datetime(row['date']) + timedelta(days=random.randint(0, 2))

            bank_entries.append({
                'date': date.strftime('%Y-%m-%d'),
                'amount': amount,
                'description': row['description'] + f" - {row['reference']}",
                'reference': row['reference'],
                'bank_reference': f'TXN{random.randint(100000, 999999)}',
            })

    for i in range(int(len(gl_df) * (1 - match_rate))):
        date = (datetime.now() - timedelta(days=random.randint(0, 30))).strftime('%Y-%m-%d')
        amount = random.randint(1000, 10000)
        bank_entries.append({
            'date': date, 'amount': amount, 'description': f'Bank Transfer {i+1}',
            'reference': f'UNMATCHED{i+1}', 'bank_reference': f'TXN{random.randint(100000, 999999)}',
        })

    df = pd.DataFrame(bank_entries)
    return df.sort_values('date').reset_index(drop=True)


if __name__ == '__main__':
    gl_data = generate_gl_data(50)
    gl_data.to_csv('sample_gl_data.csv', index=False)
    bank_data = generate_bank_data(gl_data, match_rate=0.80)
    bank_data.to_csv('sample_bank_data.csv', index=False)
    print(f"Generated {len(gl_data)} GL entries and {len(bank_data)} bank entries")
