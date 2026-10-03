"""Project entrypoint.

Examples:
    python run.py
    python run.py --execute --confirm-calibration
    python run.py --navigation-only --execute
"""
from app.main import main

if __name__ == "__main__":
    main()
