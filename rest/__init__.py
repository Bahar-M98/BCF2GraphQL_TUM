"""
rest/__init__.py — REST API router registry.

BCF sub-router is mounted here with its canonical URL prefix:

  BCF  →  /bcf/3.0/...   buildingSMART BCF REST API 3.0 spec

The BCF REST API is the official buildingSMART standard and serves as the
legitimate baseline for benchmarking against GraphQL.

main.py mounts this router at the root (no additional prefix).
"""

from fastapi import APIRouter
from rest.bcf import router as bcf_router

router = APIRouter()
router.include_router(bcf_router, prefix="/bcf/3.0")
