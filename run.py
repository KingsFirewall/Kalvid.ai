#!/usr/bin/env python3
"""Start the Kalvid AI dashboard.  python run.py"""
import uvicorn

from app.config import settings

if __name__ == "__main__":
    banner = "DRY RUN — mock provider, nothing billable" if settings.dry_run \
             else "*** LIVE — provider calls will be CHARGED ***"
    print(f"\n  Kalvid AI  |  {banner}")
    print(f"  http://{settings.host}:{settings.port}\n")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
