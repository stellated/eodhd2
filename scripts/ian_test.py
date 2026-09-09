from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pandas as pd
from email_downloader import *
from tips_io import parse_tip_email, parse_tip_emails, tips_exchange2sqlite, tips_sqlite2pandas
from utils import get_file_prefix, save_html, save_csvs
from eodhd_io import Database, pandas2polars

load_dotenv()

IMAP_SERVER = os.environ["imap_server"]
USERNAME = os.environ["imap_username"]
PASSWORD = os.environ["imap_password"]
SENDER_EMAIL = "reports@stockdataanalytics.com"

TESTING = True
HTML_FOLDER = None
CSV_FOLDER = None
eodhd_api_token = os.environ["EODHD_API_TOKEN"]
if TESTING:
    EMAIL_FOLDER = Path("../scripts/data/eml")  # default
    HTML_FOLDER = Path("../scripts/data/html")
    CSV_FOLDER = Path("../scripts/data/csv")
    DB_FILE = Path("../scripts/data/test.db")
    DELETE_DB = True
else:
    print(os.getenv("system"))
    if os.getenv("system"):
        if os.getenv("system") == "sirius":
            EMAIL_FOLDER = Path(os.getenv("DATA_DIR")) / 'emails'
    else:
        print("os.getenv('system') does not exist")
    print("EMAIL_FOLDER", trim_dir(EMAIL_FOLDER))
# Create target folder if it doesn't exist
if EMAIL_FOLDER.is_dir():
    print(f"saving emails to: {trim_dir(EMAIL_FOLDER)}")
else:
    print(f"saving emails to: {trim_dir(EMAIL_FOLDER)}, (which doesn't exist, creating now)")
    EMAIL_FOLDER.mkdir()

start = datetime.now()
print('starting', start)

if DB_FILE:
    db = Database(DB_FILE, eodhd_api_token, delete_db = DELETE_DB)

for eml_file in sorted(list(EMAIL_FOLDER.glob("*.eml"))):
    file_prefix = get_file_prefix(eml_file)
    if not(file_prefix.startswith("2026-04-08") or file_prefix == "2026-04-09"):
        break

    print()
    print('***', file_prefix.ljust(17), end='\t')

    if HTML_FOLDER:
        save_html(eml_file, HTML_FOLDER, file_prefix)
        print('saved html', end='\t')

    exchange_pdf, tips_pdf = parse_tip_email(eml_file)
    print('parsed', end='\t')

    if CSV_FOLDER:
        save_csvs(exchange_pdf, tips_pdf, CSV_FOLDER, file_prefix)
        print('saved csv', end='\t')

    if DB_FILE:
        tips_exchange2sqlite(exchange_pdf, tips_pdf, db)
        tips_df = pandas2polars(tips_pdf)
        for i, tip in enumerate(tips_df.iter_rows(named=True)):  # named=True yields dicts instead of tuples
            if i > 4:
                break
            print(tip)
            db.fetch(
                tip['code'],
                '1d',
                'daily',
                tip['tip_date'] - timedelta(days=10),
                tip['tip_date'] + timedelta(days=10),
            )
            db.fetch(
                tip['code'],
                '5m',
                'fiveminutely',
                tip['tip_date'] - timedelta(days=10),
                tip['tip_date'] + timedelta(days=10),
            )
        # exchange_df = pandas2polars(exchange_pdf)
        # for row in exchange_df.iter_rows(named=True):
        #     print(row)

        raise Exception("DONE")
    print()

print('done extracting', datetime.now())
print('finished, took', datetime.now() - start)


