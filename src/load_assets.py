"""Load scraped listing JSON into the PostgreSQL assets table."""

from __future__ import annotations

import argparse
from configparser import ConfigParser
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from psycopg.conninfo import make_conninfo

from db import Inserter
from models import PropertyObject


ASSET_COLUMNS = [
    "city_name",
    "neighborhood_name",
    "source",
    "source_listing_id",
    "latitude",
    "longitude",
    "price",
    "size",
    "num_bedrooms",
    "metadata",
    "url",
    "scraped_at",
]
logger = logging.getLogger(__name__)
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def load_json(path: str | Path) -> list[PropertyObject]:
    """Read a JSON array and skip records that cannot form a listing model."""
    with Path(path).open(encoding="utf-8") as file:
        payload: Any = json.load(file)

    if not isinstance(payload, list):
        raise ValueError("Input JSON must contain an array of listings")
    assets = []
    invalid = 0
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            invalid += 1
            logger.warning("Skipping non-object listing at %s[%d]", path, index)
            continue
        try:
            assets.append(PropertyObject.from_scraped_dict(item))
        except (TypeError, ValueError) as error:
            invalid += 1
            logger.warning("Skipping invalid listing at %s[%d]: %s", path, index, error)
    if invalid:
        logger.info("Skipped %d invalid listings from %s", invalid, path)
    return assets


def assets_to_dataframe(assets: list[PropertyObject]) -> pd.DataFrame:
    """Map listings to DB columns and drop rows missing required DB values."""
    scraped_at = datetime.now(timezone.utc)
    rows = []
    for asset in assets:
        rows.append(
            {
                "city_name": asset.city,
                "neighborhood_name": asset.neighborhood,
                "source": "idealista",
                "source_listing_id": asset.id,
                "latitude": asset.lat,
                "longitude": asset.lon,
                "price": asset.price,
                "size": asset.size,
                "num_bedrooms": _bedroom_count(asset),
                "metadata": asset.model_dump(mode="json", by_alias=True),
                "url": asset.url,
                "scraped_at": scraped_at,
            }
        )
    dataframe = pd.DataFrame(rows, columns=ASSET_COLUMNS)
    required = [
        "city_name",
        "source_listing_id",
        "price",
        "size",
    ]
    for column in ("city_name", "neighborhood_name", "source_listing_id"):
        dataframe[column] = dataframe[column].replace(r"^\s*$", pd.NA, regex=True)
    incomplete = dataframe[required].isna().any(axis=1)
    skipped = int(incomplete.sum())
    if skipped:
        logger.warning(
            "Skipping %d listings missing required DB fields: %s",
            skipped,
            ", ".join(required),
        )
    return dataframe.loc[~incomplete].reset_index(drop=True)


async def insert_json(path: str | Path, *, service: str | None = None) -> int:
    """Load listings, add missing locations, then insert assets."""
    assets = await asyncio.to_thread(load_json, path)
    dataframe = assets_to_dataframe(assets)
    db_service = service or "local"
    inserted_cities, inserted_neighborhoods = await _insert_missing_locations(
        dataframe, service=db_service
    )
    inserted_assets = await _insert_assets(dataframe, service=db_service)
    logger.info(
        "Inserted %d cities, %d neighborhoods, and %d asset rows",
        inserted_cities,
        inserted_neighborhoods,
        inserted_assets,
    )
    return inserted_assets


async def insert_directory(path: str | Path, *, service: str | None = None) -> int:
    """Load every JSON listings file in a directory, in filename order."""
    directory = Path(path)
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    files = sorted(directory.glob("*.json"))
    if not files:
        logger.warning("No JSON files found in %s", directory)
        return 0

    total = 0
    for json_file in files:
        count = await insert_json(json_file, service=service)
        logger.info("Loaded %s: %d assets", json_file.name, count)
        total += count
    return total


async def _insert_missing_locations(
    dataframe: pd.DataFrame, *, service: str
) -> tuple[int, int]:
    """Insert distinct cities and city/neighborhood pairs that are not in DB."""
    if dataframe.empty:
        return 0, 0

    cities = dataframe[["city_name"]].drop_duplicates().rename(
        columns={"city_name": "name"}
    )
    neighborhoods = dataframe[["city_name", "neighborhood_name"]].dropna(
        subset=["neighborhood_name"]
    ).drop_duplicates().rename(
        columns={"city_name": "city_name", "neighborhood_name": "name"}
    )

    inserter = _connect_inserter(service)
    try:
        inserted_cities = inserter.insert(
            "cities", cities, on_conflict_do_nothing=True
        )
        inserted_neighborhoods = inserter.insert(
            "neighborhoods", neighborhoods, on_conflict_do_nothing=True
        )
        inserter.conn.commit()
    except Exception:
        inserter.conn.rollback()
        raise
    finally:
        inserter.close()
    return inserted_cities, inserted_neighborhoods


async def _insert_assets(dataframe: pd.DataFrame, *, service: str) -> int:
    """Insert asset DataFrame rows using a separate async Inserter."""
    if dataframe.empty:
        return 0

    inserter = _connect_inserter(service)
    try:
        inserted = inserter.insert(
            "assets", dataframe, on_conflict_do_nothing=True
        )
        inserter.conn.commit()
    except Exception:
        inserter.conn.rollback()
        raise
    finally:
        inserter.close()
    return inserted


def _connect_inserter(service: str) -> Inserter:
    """Connect using the selected section in this project's pgser.conf."""
    service_file = Path(__file__).resolve().parents[1] / "pgser.conf"
    config = ConfigParser()
    if not config.read(service_file):
        raise FileNotFoundError(f"Database service file not found: {service_file}")
    if service not in config:
        raise ValueError(f"Database service [{service}] not found in {service_file}")
    conninfo = make_conninfo(**dict(config[service]))
    return Inserter().connect(conninfo=conninfo)


def _bedroom_count(asset: PropertyObject) -> int | None:
    """Extract bedroom count from Idealista's feature strings, if present."""
    # Bedroom count is not modeled separately yet; it remains in the metadata.
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help="JSON listings file or folder (default: %(default)s)",
    )
    parser.add_argument("--service", help="Override DB_SERVICE from .env")
    args = parser.parse_args()
    if args.path.is_dir():
        count = asyncio.run(insert_directory(args.path, service=args.service))
    elif args.path.is_file():
        count = asyncio.run(insert_json(args.path, service=args.service))
    else:
        parser.error(f"Path does not exist: {args.path}")
    print(f"Inserted {count} assets")


if __name__ == "__main__":
    main()
