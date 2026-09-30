import email
import imaplib
import os
import re
from datetime import datetime
from email.header import decode_header
from pathlib import Path

from dotenv import load_dotenv


def trim_dir(folder):
    # just to make the prints a bit more readable
    return str(folder).replace("/Users/ianatkinson/Library/CloudStorage/OneDrive-Personal", "~")

def sanitize_filename(filename):
    """Sanitize the filename to remove invalid characters."""
    # return re.sub(r'[\\/*?:"<>|]', "", filename)
    return re.sub(r'[^a-zA-Z0-9\s\-]', '', filename)  # remove that silly emoji

def decode_subject(subject):
    decoded_parts = decode_header(subject)
    decoded_subject = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            decoded_subject.append(part.decode(encoding or "utf-8"))
        else:
            decoded_subject.append(part)
    return "".join(decoded_subject)

def download_emails(imap_server,
                    username,
                    password,
                    target_folder,
                    sender_email,
                    write=True,
                    n=None,
                    next_n=None,
                    unseen_only=False,
                    mark_seen=True):
    # Connect to the IMAP server
    mail = imaplib.IMAP4_SSL(imap_server)
    mail.login(username, password)
    # mail.select("_Shares")
    mail.select("Inbox/SDA")

    # Search for emails from the specified sender. unseen_only=True is for
    # unattended/scheduled runs against a scratch mailbox: combined with the
    # \\Seen flag this function sets below (when mark_seen=True), it means
    # each run only re-processes emails that arrived since the last
    # successful run, instead of re-downloading the whole mailbox history
    # every time (relevant when the caller has no local record of what it
    # already saw, e.g. a stateless VM that doesn't keep previously-
    # downloaded .eml files around).
    #
    # \\Seen is shared IMAP-server state, not per-caller: mark_seen and
    # unseen_only are independent knobs, but a second, independent consumer
    # of the same mailbox (not just the same unattended scratch VM) needs
    # BOTH unseen_only=False (see everything, not just what it personally
    # left unread) AND mark_seen=False (don't consume what the *other*
    # consumer still needs to see as unread) -- setting only the former
    # still lets it silently steal new mail out from under the other
    # consumer by marking it Seen.
    search_criteria = f'(FROM "{sender_email}")'
    if unseen_only:
        search_criteria = f'(FROM "{sender_email}" UNSEEN)'
    status, messages = mail.search(None, search_criteria)
    if status != "OK":
        print("No messages found!")
        return

    email_ids = messages[0].split()
    print(len(email_ids), "messages found")

    new_download_count = 0
    for i, email_id in enumerate(email_ids):
        if i == n:
            break
        if new_download_count == next_n:
            break
        print('fetching', str(i).ljust(3), '\t', end='')

        # Fetch the email
        status, msg_data = mail.fetch(email_id, "(RFC822)")
        if status != "OK":
            continue

        raw_email = msg_data[0][1]
        email_message = email.message_from_bytes(raw_email)

        # Extract date and subject
        date_str = email_message["Date"]
        subject = email_message["Subject"] or "no_subject"
        if date_str:
            date_obj = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
        else:
            # extract date from message header
            parts = msg_data[0][1].decode('utf-8', errors='replace').split("Received")
            second_part = parts[1]
            third_line = second_part.split('\n')[2].strip()
            date_obj = datetime.strptime(third_line, "%a, %d %b %Y %H:%M:%S %z")
        date_str = date_obj.strftime("%Y-%m-%d_%H-%M-%S")

        # Clean up subject for filename
        decoded_subject = decode_subject(subject)
        sanitized_subject = sanitize_filename(decoded_subject)
        pick_ptr = sanitized_subject.find('Pick')
        clean_subject = sanitized_subject[:pick_ptr + 4].strip()
        filename = f"{date_str}_{clean_subject}.eml"
        print(filename)
        filepath = os.path.join(target_folder, filename)

        # Skip if file already exists
        if os.path.exists(filepath):
            print(f"\tnot saving {filename} (already exists)")
            continue

        # Save the email to a file
        if write:
            with open(filepath, "wb") as f:
                f.write(msg_data[0][1])
            print(f"\tsaving {filename} to {trim_dir(target_folder)}")
            if mark_seen:
                mail.store(email_id, "+FLAGS", "\\Seen")
            new_download_count += 1
        else:
            print(f"not downloading: {filename} to _{trim_dir(target_folder)} because write=False")

    mail.close()
    mail.logout()


if __name__ == '__main__':
    # Configuration
    load_dotenv()
    IMAP_SERVER = os.environ["imap_server"]
    USERNAME = os.environ["imap_username"]
    PASSWORD = os.environ["imap_password"]

    '''
    Original plan was to pull emails from gmail, but that just gets errors
    So instead gmail forwards to ian@atkinson.id.au and Ventra's Axigen puts them into a subfolder
    '''

    TARGET_FOLDER = Path("../emails")  # default
    if os.getenv("system") == "sirius":
        TARGET_FOLDER = Path(os.getenv("DATA_DIR")) / 'emails'
    elif not os.getenv("system"):
        print("os.getenv('system') does not exist")

    SENDER_EMAIL = "reports@stockdataanalytics.com"

    # Create target folder if it doesn't exist
    if TARGET_FOLDER.is_dir():
        print(f"saving emails to: {TARGET_FOLDER}")
    else:
        print(f"saving emails to: {TARGET_FOLDER}, (which doesn't exist, creating now)")
        TARGET_FOLDER.mkdir(parents=True)

    # Run the script
    download_emails(IMAP_SERVER, USERNAME, PASSWORD, TARGET_FOLDER, SENDER_EMAIL)
