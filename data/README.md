# Backtest data

This folder contains small universe metadata, research outputs, and large historical market-data caches.

The local cache files in `funding/`, `klines/`, `klines_15m/`, and the top-level `.npy`/`.npz` files remain on disk and are excluded from ordinary Git commits. They currently account for most of this folder's roughly 962 MB. The scripts in the project download and assemble these datasets, but the exact historical universe and available exchange records can change over time.

The small top-level JSON metadata and `results/` outputs remain eligible for version control. Before sharing or reproducing a result, record its script, input universe, date range, and relevant configuration. Use a dedicated data store or Git LFS if another machine needs the full cached datasets.
