"""Verify one staged/installed client without opening accounts or model settings."""
import argparse
import json
from pathlib import Path

from msvc_runtime import specification, verify_deployment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify_deployment(args.client, specification(Path(__file__).resolve().parents[1]))
    except (OSError, ValueError) as error:
        parser.exit(1, "Native runtime verification rejected: " + str(error) + "\n")
    print(json.dumps(report, ensure_ascii=True))


if __name__ == "__main__":
    main()
