"""
main.py
=======
Entry point. Collects datasets from all configured source modules
and hands them to the central pipeline.

To add a new data source:
  1. Create sources/your_source.py implementing get_datasets() -> list[dict]
  2. Import it here and add it to SOURCES
"""

from sources import os_api
from pipeline import run_pipeline

# ---------------------------------------------------------------------------
# Register sources — each must expose get_datasets() -> list[dict]
# ---------------------------------------------------------------------------

SOURCES = [
    os_api,
    # another_api,  # add future sources here
]

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    datasets = []
    for source in SOURCES:
        try:
            source_datasets = source.get_datasets()
            print(f"[{source.__name__}] Found {len(source_datasets)} datasets")
            datasets.extend(source_datasets)
        except Exception as e:
            print(f"[{source.__name__}] ERROR fetching dataset list: {e}")

    if not datasets:
        print("No datasets to process — exiting")
        raise SystemExit(1)

    run_pipeline(datasets)
