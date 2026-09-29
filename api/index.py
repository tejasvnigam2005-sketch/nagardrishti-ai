import sys
from pathlib import Path

# Add project root and backend directory to sys.path so modules can be imported
root_dir = Path(__file__).resolve().parent.parent
backend_dir = root_dir / "backend"

for path in [str(backend_dir), str(root_dir)]:
    if path not in sys.path:
        sys.path.insert(0, path)

# Import the FastAPI application instance
from backend.main import app

# Export app for Vercel Serverless Function runner
__all__ = ["app"]
