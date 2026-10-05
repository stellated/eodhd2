from pathlib import Path

from numpy.linalg.lapack_lite import dgelsd

from eodhd_io import Database

data_dir = Path('/Users/ianatkinson/Library/CloudStorage/OneDrive-Personal/Data/StockDataAnalytics/mars')
db1 = Database(data_dir / 'sda.2026-09-30.db')
df1 = db1.to_polars('daily')

db2 = Database(data_dir / 'sda.2026-10-05.db')
df2 = db2.to_polars('daily')

new_in_2 = df2.join(df1, on=['code', 'timestamp'])
print(new_in_2)
