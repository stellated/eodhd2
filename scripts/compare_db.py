from pathlib import Path
import polars as pl
from eodhd_io import Database

data_dir = Path('/Users/ianatkinson/Library/CloudStorage/OneDrive-Personal/Data/StockDataAnalytics/mars')
db1 = Database(data_dir / 'sda.2026-09-30.db')
df1 = db1.to_polars('daily')

db2 = Database(data_dir / 'sda.2026-10-05.db')
df2 = db2.to_polars('daily')

new_in_2 = df2.join(df1, on=['code', 'timestamp'], how='anti')
print(new_in_2)

d = {}
new_codes = sorted(list(set(new_in_2['code'])))
print(len(new_codes), 'new codes')
for i, code in enumerate(new_codes):
    print(code, len(new_in_2.filter(pl.col('code') == code)))
    length = len(new_in_2.filter(pl.col('code') == code))
    if length in d:
        d[length] += 1
    else:
        d[length] = 1
for length in sorted(d.keys()):
    print(length, d[length])

with (pl.Config(tbl_cols=-1, tbl_rows=-1)):
    print(
        new_in_2.filter(pl.col('code') == 'AAOI.US')
        .select('code', 'date')
        .with_columns(pl.col('date').dt.strftime("%A").alias('weekday'))
    )

