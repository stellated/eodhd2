from datetime import datetime, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pandas as pd
from email_downloader import *
from tips_io import parse_tip_email, parse_tip_emails, tips_exchange2sqlite, tips_sqlite2pandas
from utils import get_file_prefix, save_html, save_csvs

load_dotenv()

IMAP_SERVER = os.environ["imap_server"]
USERNAME = os.environ["imap_username"]
PASSWORD = os.environ["imap_password"]
SENDER_EMAIL = "reports@stockdataanalytics.com"

TESTING = True

HTML_FOLDER = None
CSV_FOLDER = None
if TESTING:
    EMAIL_FOLDER = Path("../tests/data/eml")  # default
    HTML_FOLDER = Path("../tests/data/html")
    CSV_FOLDER = Path("../tests/data/csv")
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


# download emails,
# optional n limits how many emails to fetch (for testing)
# optional next_n limits how many new emails it downloads (for testing)
download_emails(
    IMAP_SERVER, USERNAME, PASSWORD, EMAIL_FOLDER, SENDER_EMAIL, n=None, next_n=None)
print('done downloading', datetime.now())

# testing extraction of data from emails
for eml_file in sorted(list(EMAIL_FOLDER.glob("*.eml"))):
    file_prefix = get_file_prefix(eml_file)
    print('***', file_prefix.ljust(17), end='\t')

    if HTML_FOLDER:
        save_html(eml_file, HTML_FOLDER, file_prefix)
        print('saved html', end='\t')

    exchange_df, tips_df = parse_tip_email(eml_file)
    print('parsed', end='\t')

    if CSV_FOLDER:
        save_csvs(exchange_df, tips_df, CSV_FOLDER, file_prefix)
        print('saved csv', end='\t')

    print()

print('done extracting', datetime.now())
print('finished, took', datetime.now() - start)


