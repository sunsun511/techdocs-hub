"""Fetch pinned model assets, verify every byte range and SHA; never update a revision."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import hashlib
import json
import httpx

ROOT = Path(__file__).resolve().parents[1]


def fetch(asset):
    target = ROOT / "models" / asset["family"] / asset["filename"]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size == asset["bytes"] and hashlib.sha256(target.read_bytes()).hexdigest() == asset["sha256"]:
        print("Verified cached", asset["filename"], flush=True)
        return
    url = f"https://huggingface.co/{asset['repo']}/resolve/{asset['revision']}/{asset['remote_path']}?download=true"
    part = target.with_suffix(target.suffix + ".part")
    size = asset["bytes"]
    if size > 1024 * 1024:
        step = 4 * 1024 * 1024
        def segment(start):
            end = min(size, start + step) - 1
            for attempt in range(3):
                try:
                    with httpx.Client(timeout=45, follow_redirects=True) as client:
                        r = client.get(url + f"&segment={start}", headers={"Range": f"bytes={start}-{end}"})
                        r.raise_for_status()
                        assert r.status_code == 206 and len(r.content) == end - start + 1
                        assert r.headers.get("content-range", "").startswith(f"bytes {start}-{end}/")
                        return start, r.content
                except Exception:
                    if attempt == 2:
                        raise
        with part.open("wb") as stream:
            stream.truncate(size)
            with ThreadPoolExecutor(max_workers=4) as pool:
                done = 0
                for future in as_completed([pool.submit(segment, i) for i in range(0, size, step)]):
                    start, data = future.result()
                    stream.seek(start)
                    stream.write(data)
                    done += len(data)
                    print(asset["filename"], round(100 * done / size), "%", flush=True)
    else:
        response = httpx.get(url, timeout=45, follow_redirects=True)
        response.raise_for_status()
        part.write_bytes(response.content)
    assert part.stat().st_size == size and hashlib.sha256(part.read_bytes()).hexdigest() == asset["sha256"], target
    part.replace(target)


def main():
    manifest = (ROOT / "fixtures/model-manifest.json").read_text(encoding="utf-8")
    for asset in json.loads(manifest)["assets"]:
        fetch(asset)
    (ROOT / "models/manifest.json").write_text(manifest, encoding="utf-8")
    print("All fixed model assets verified.")


if __name__ == "__main__":
    main()
