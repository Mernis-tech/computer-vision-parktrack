"""Decrypt a locally downloaded PTSNAP01 camera snapshot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .snapshot_storage import decode_encryption_key, decrypt_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("encrypted_file", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перезаписать существующий выходной файл",
    )
    args = parser.parse_args()

    if args.output_file.exists() and not args.force:
        parser.error(f"output file already exists: {args.output_file}")

    key = decode_encryption_key(
        os.environ.get("SNAPSHOT_ENCRYPTION_KEY_BASE64", "").strip()
    )
    header, plaintext = decrypt_snapshot(args.encrypted_file.read_bytes(), key)

    args.output_file.parent.mkdir(parents=True, exist_ok=True)
    args.output_file.write_bytes(plaintext)
    print(json.dumps(header, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
