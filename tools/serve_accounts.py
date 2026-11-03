import argparse
import uvicorn
from app.auth import create_app

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    app = create_app(args.database, "http://127.0.0.1:" + str(args.port))
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="error", proxy_headers=False
    )
