from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Export and optionally delete all people from a folk workspace."
    )
    parser.add_argument("--execute", action="store_true", help="Actually delete people after exporting them.")
    parser.add_argument(
        "--confirm",
        default="",
        help="Required with --execute. Must be DELETE_ALL_FOLK_PEOPLE.",
    )
    parser.add_argument(
        "--output-dir",
        default="exports",
        help="Directory where the folk people backup JSON is written.",
    )
    parser.add_argument(
        "--spacing",
        type=float,
        default=float(os.environ.get("KONDO_FOLK_REQUEST_SPACING_SECONDS", "0.5")),
        help="Seconds to sleep between delete requests.",
    )
    args = parser.parse_args()

    api_key = os.environ.get("FOLK_API_KEY")
    if not api_key:
        raise SystemExit("FOLK_API_KEY is required in the environment or .env")

    if args.execute and args.confirm != "DELETE_ALL_FOLK_PEOPLE":
        raise SystemExit("--execute requires --confirm DELETE_ALL_FOLK_PEOPLE")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = output_dir / f"folk_people_backup_{stamp}.json"

    with httpx.Client(
        base_url=os.environ.get("FOLK_BASE_URL", "https://api.folk.app/v1"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    ) as client:
        people = list_people(client)
        backup_path.write_text(json.dumps(people, indent=2, sort_keys=True, default=str), encoding="utf-8")
        print(f"Exported {len(people)} folk people to {backup_path}")

        if not args.execute:
            print("Dry run only. Re-run with --execute --confirm DELETE_ALL_FOLK_PEOPLE to delete them.")
            return

        deleted = 0
        for person in people:
            person_id = str(person.get("id") or "")
            full_name = str(person.get("fullName") or person_id)
            if not person_id:
                continue
            response = client.delete(f"/people/{person_id}")
            if response.status_code == 429:
                retry_after = float(response.headers.get("retry-after") or "5")
                time.sleep(max(retry_after, args.spacing))
                response = client.delete(f"/people/{person_id}")
            response.raise_for_status()
            deleted += 1
            print(f"Deleted {deleted}/{len(people)}: {full_name} ({person_id})")
            if args.spacing > 0:
                time.sleep(args.spacing)
        print(f"Deleted {deleted} folk people.")


def list_people(client: httpx.Client) -> list[dict[str, object]]:
    people: list[dict[str, object]] = []
    path = "/people"
    params: dict[str, str | int] | None = {"limit": 100}
    while True:
        response = client.get(path, params=params)
        response.raise_for_status()
        data = response.json().get("data") or {}
        items = data.get("items") or []
        people.extend(item for item in items if isinstance(item, dict))
        next_link = (data.get("pagination") or {}).get("nextLink")
        if not next_link:
            break
        parsed = urlparse(str(next_link))
        path = parsed.path.removeprefix("/v1") or "/people"
        query = parse_qs(parsed.query)
        params = {key: values[-1] for key, values in query.items() if values}
    return people


if __name__ == "__main__":
    main()
