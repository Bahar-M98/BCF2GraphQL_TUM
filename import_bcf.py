"""
CLI tool to import one or more BCF files into MongoDB.

Usage:
    uv run python import_bcf.py path/to/file.bcf [file2.bcf ...]
"""

import asyncio
import sys
from pathlib import Path

from bcf_parser import parse_bcf
from db.database import save_bcf, save_project

async def import_one(filepath: str):
    filename = Path(filepath).stem
    print(f"\nParsing {filepath}...")
    data = parse_bcf(filepath)
    print(f"  Found {len(data['topics'])} topics")
    project = data.get("project") or {}
    project_id = await save_project(project, filename)
    print(f"  Project '{project_id}' saved")
    await save_bcf(filename, data, project_id)
    print("  Done.")

async def main():
    if len(sys.argv) < 2:
        print("Usage: python import_bcf.py <file1.bcf> [file2.bcf ...]")
        sys.exit(1)

    for filepath in sys.argv[1:]:
        await import_one(filepath)

asyncio.run(main())
