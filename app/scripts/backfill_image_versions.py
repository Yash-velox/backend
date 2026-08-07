"""Repeatable backfill of ORIGINAL image_versions from product_media.

Usage (from Backend/):
  .venv/bin/python -m app.scripts.backfill_image_versions [--shop-domain DOMAIN]
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.db.session import SessionLocal
from app.models import Shop
from app.services.image_versions import backfill_originals_for_shop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backfill_image_versions")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill ORIGINAL image_versions from catalog media")
    parser.add_argument("--shop-domain", default=None, help="Limit to one shop domain")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        q = db.query(Shop)
        if args.shop_domain:
            q = q.filter(Shop.shop_domain == args.shop_domain)
        shops = q.all()
        if not shops:
            logger.error("No shops found")
            return 1
        for shop in shops:
            result = backfill_originals_for_shop(db, shop)
            logger.info(
                "Backfill complete | shop=%s products=%s media=%s created=%s",
                shop.shop_domain,
                result["products"],
                result["mediaScanned"],
                result["originalsCreated"],
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
