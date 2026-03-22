#!/usr/bin/env python3
"""
Zillow House Monitor - Main entry point.

Monitors Zillow listings based on configurable criteria, evaluates them
using an LLM for aesthetic qualities (kitchen, yard, appearance), and
sends email notifications for matching listings.

Usage:
    python monitor.py                  # Run once
    python monitor.py --loop           # Run continuously on schedule
    python monitor.py --config my.yaml # Use custom config file
    python monitor.py --dry-run        # Search without sending notifications
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

from evaluator import evaluate_listings
from notifier import send_email_notification
from scraper import Listing, search_listings

logger = logging.getLogger("zillow_monitor")


def load_config(config_path: str = "config.yaml") -> dict:
    """Load and validate configuration from YAML file."""
    path = Path(config_path)
    if not path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Copy config.yaml.example to config.yaml and edit it.")
        sys.exit(1)

    with open(path) as f:
        config = yaml.safe_load(f)

    # Validate required fields
    if "search" not in config:
        print("Error: 'search' section missing from config")
        sys.exit(1)
    if not config["search"].get("location"):
        print("Error: 'search.location' is required")
        sys.exit(1)

    return config


def setup_logging(config: dict) -> None:
    """Configure logging based on config."""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)
    log_file = log_config.get("file")

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def load_seen_listings(path: str) -> set[str]:
    """Load previously seen listing IDs from disk."""
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
            return set(data.get("seen", []))
    return set()


def save_seen_listings(path: str, seen: set[str]) -> None:
    """Save seen listing IDs to disk."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"seen": list(seen), "updated": datetime.now().isoformat()}, f, indent=2)


def save_results(results_dir: str, listings: list[tuple[Listing, object]]) -> str:
    """Save listing results to a JSON file. Returns the file path."""
    os.makedirs(results_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(results_dir, f"results_{timestamp}.json")

    data = []
    for listing, evaluation in listings:
        entry = {
            "zpid": listing.zpid,
            "address": listing.address,
            "price": listing.price,
            "beds": listing.beds,
            "baths": listing.baths,
            "sqft": listing.sqft,
            "home_type": listing.home_type,
            "year_built": listing.year_built,
            "days_on_zillow": listing.days_on_zillow,
            "url": listing.url,
            "description": listing.description[:500] if listing.description else "",
        }
        if evaluation:
            entry["evaluation"] = {
                "scores": evaluation.scores,
                "reasoning": evaluation.reasoning,
                "summary": evaluation.summary,
                "passes": evaluation.passes,
            }
        data.append(entry)

    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

    logger.info("Results saved to %s", filepath)
    return filepath


def run_monitor(config: dict, dry_run: bool = False) -> int:
    """
    Run one monitoring cycle.

    Returns the number of new listings found.
    """
    monitor_config = config.get("monitor", {})
    track_seen = monitor_config.get("track_seen_listings", True)
    seen_file = monitor_config.get("seen_listings_file", "data/seen_listings.json")
    results_dir = monitor_config.get("results_dir", "data/results")

    # Load previously seen listings
    seen = load_seen_listings(seen_file) if track_seen else set()
    logger.info("Loaded %d previously seen listings", len(seen))

    # Search Zillow
    logger.info("=" * 60)
    logger.info("Starting Zillow search...")
    all_listings = search_listings(config)
    logger.info("Found %d listings from Zillow", len(all_listings))

    if not all_listings:
        logger.info("No listings found matching search criteria")
        return 0

    # Filter out previously seen listings
    if track_seen:
        new_listings = [l for l in all_listings if l.zpid not in seen]
        logger.info("%d new listings (not previously seen)", len(new_listings))
    else:
        new_listings = all_listings

    if not new_listings:
        logger.info("No new listings since last check")
        return 0

    # Evaluate listings with LLM
    evaluated = evaluate_listings(new_listings, config)
    logger.info("%d listings passed LLM evaluation", len(evaluated))

    # Save results
    if evaluated:
        save_results(results_dir, evaluated)

    # Send notifications
    if not dry_run and evaluated:
        send_email_notification(evaluated, config)
    elif dry_run:
        logger.info("[DRY RUN] Would send notification for %d listings", len(evaluated))
        for listing, evaluation in evaluated:
            url = listing.url if listing.url.startswith("http") else f"https://www.zillow.com{listing.url}"
            print(f"\n  {listing.address}")
            print(f"  ${listing.price:,} | {listing.beds}bd/{listing.baths}ba | {listing.sqft or '?'} sqft")
            print(f"  {url}")
            if evaluation:
                for name, score in evaluation.scores.items():
                    print(f"  {name}: {score}/10 - {evaluation.reasoning.get(name, '')}")

    # Mark all found listings as seen (even filtered ones, to avoid re-evaluating)
    if track_seen:
        for listing in all_listings:
            seen.add(listing.zpid)
        save_seen_listings(seen_file, seen)

    return len(evaluated)


def main():
    parser = argparse.ArgumentParser(
        description="Monitor Zillow for houses matching your criteria",
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously on the configured schedule",
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Search and evaluate but don't send notifications",
    )
    parser.add_argument(
        "--reset-seen",
        action="store_true",
        help="Clear the seen listings cache before running",
    )

    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(config)

    if args.reset_seen:
        seen_file = config.get("monitor", {}).get("seen_listings_file", "data/seen_listings.json")
        if os.path.exists(seen_file):
            os.remove(seen_file)
            logger.info("Cleared seen listings cache")

    if args.loop:
        interval = config.get("monitor", {}).get("interval_minutes", 60)
        logger.info("Starting continuous monitoring (every %d minutes)", interval)

        while True:
            try:
                found = run_monitor(config, dry_run=args.dry_run)
                logger.info("Cycle complete. Found %d matching listings.", found)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
            except Exception:
                logger.error("Error during monitoring cycle", exc_info=True)

            logger.info("Sleeping %d minutes until next check...", interval)
            try:
                time.sleep(interval * 60)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
                break
    else:
        found = run_monitor(config, dry_run=args.dry_run)
        logger.info("Done. Found %d matching listings.", found)


if __name__ == "__main__":
    main()
