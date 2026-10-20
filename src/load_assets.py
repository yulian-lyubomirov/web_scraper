"""Load scraped listing JSON into the PostgreSQL assets table."""

from __future__ import annotations

import argparse
import json
import logging
import os
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

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


def load_json(path: str | Path) -> list[PropertyObject]:
    """Read a JSON array of scraped listings and validate each as an asset."""
    with Path(path).open(encoding="utf-8") as file:
        payload: Any = json.load(file)

    if not isinstance(payload, list):
        raise ValueError("Input JSON must contain an array of listings")
    return [PropertyObject.from_scraped_dict(item) for item in payload]


def assets_to_dataframe(assets: list[PropertyObject]) -> pd.DataFrame:
    """Map validated listings to the columns in db/migrations/init.sql."""
    scraped_at = datetime.now(timezone.utc)
    rows = []
    for asset in assets:
        if asset.price is None or asset.size is None:
            raise ValueError(f"Listing {asset.id} must have both price and size")
        if not asset.city or not asset.neighborhood:
            raise ValueError(f"Listing {asset.id} must have city and neighborhood")

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
    return pd.DataFrame(rows, columns=ASSET_COLUMNS)


async def insert_json(path: str | Path, *, service: str | None = None) -> int:
    """Load listings, add missing locations, then insert assets."""
    assets = await asyncio.to_thread(load_json, path)
    dataframe = assets_to_dataframe(assets)
    service_file = Path(__file__).resolve().parents[1] / "pgser.conf"
    os.environ.setdefault("PGSERVICEFILE", str(service_file))
    db_service = service or _db_service()
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


async def _insert_missing_locations(
    dataframe: pd.DataFrame, *, service: str
) -> tuple[int, int]:
    """Insert distinct cities and city/neighborhood pairs that are not in DB."""
    if dataframe.empty:
        return 0, 0

    cities = dataframe[["city_name"]].drop_duplicates().rename(
        columns={"city_name": "name"}
    )
    neighborhoods = dataframe[["city_name", "neighborhood_name"]].drop_duplicates().rename(
        columns={"city_name": "city_name", "neighborhood_name": "name"}
    )

    inserter = Inserter().connect(service=service)
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

    inserter = Inserter().connect(service=service)
    try:
        inserted = inserter.insert("assets", dataframe)
        inserter.conn.commit()
    except Exception:
        inserter.conn.rollback()
        raise
    finally:
        inserter.close()
    return inserted


def _db_service() -> str:
    """Read DB_SERVICE from the project .env file, falling back to local."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_dotenv(env_path)
    return os.getenv("DB_SERVICE", "local")


def _bedroom_count(asset: PropertyObject) -> int | None:
    """Extract bedroom count from Idealista's feature strings, if present."""
    # Bedroom count is not modeled separately yet; it remains in the metadata.
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=Path, help="Path to a JSON array of listings")
    parser.add_argument("--service", help="Override DB_SERVICE from .env")
    args = parser.parse_args()
    count = asyncio.run(insert_json(args.json_file, service=args.service))
    print(f"Inserted {count} assets")


if __name__ == "__main__":
    main()
