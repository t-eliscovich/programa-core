"""Entry point for development.

Run:
    python run.py

For production on Windows Server (EC2) use:
    waitress-serve --listen=0.0.0.0:5000 run:app
"""
from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
