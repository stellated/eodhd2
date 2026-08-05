from datetime import datetime
import pathlib
import pandas as pd
from tips_io import _get_html


def get_file_prefix(eml_file: pathlib.Path) -> str:
    html = _get_html(eml_file).decode("utf-8", errors='ignore')
    exchange = 'unknown'
    for line in html.split("\n")[:10]:
        if 'NASDAQ' in line:
            exchange = 'NASDAQ'
            break
        elif 'NYSE' in line:
            exchange = 'NYSE'
            break

    file_date = datetime.strptime(eml_file.stem.split("_")[0], "%Y-%m-%d").date()
    file_prefix = file_date.strftime("%Y-%m-%d") + '.' + exchange
    return file_prefix

def save_html(eml_file: pathlib.Path,
              html_folder: pathlib.Path,
              file_prefix: str):
    """
    Saves html page to html folder, for ian to eyeball
    """
    html = _get_html(eml_file).decode("utf-8", errors='ignore')
    html_path = html_folder / (file_prefix + '.html')
    with open(html_path, "w") as f:
        f.write(html)

def save_csvs(exchange_df: pd.DataFrame,
              tips_df: pd.DataFrame,
              csv_folder: pathlib.Path,
              file_prefix: str):
    """
    Saves pd.DataFrames
    :param exchange_df:
    :param tips_df:
    :param csv_folder:
    :param fname: str, ie: '2026-12-03.NASDAQ'
    :return:
    """

    exchange_fname = file_prefix + ".exchange.csv"
    exchange_df.to_csv(csv_folder / exchange_fname, index=False)

    tips_fname = file_prefix + ".tips.csv"
    tips_df.to_csv(csv_folder / tips_fname, index=False)
