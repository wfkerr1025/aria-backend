"""Re-download the Human Base Meshes bundle and check it against the manifest.

    python aria_models/fetch_human_base_meshes.py

The .blend is not in git (see README), so this is how a fresh clone
gets one. It verifies the sha256 rather than trusting the download:
the advertised blender.org URL is an interstitial that answers 200 with
an HTML page, and a 48KB "zip" that is really a thank-you page is
exactly the sort of thing that gets unzipped without anybody looking.
"""
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "manifest_human_base_meshes.json")


def main():
    spec = json.loads(io.open(MANIFEST, encoding="utf-8").read())
    target = os.path.join(HERE, spec["file"]["path"].replace("/", os.sep))
    wanted = spec["file"]["sha256"]

    if os.path.isfile(target):
        got = hashlib.sha256(open(target, "rb").read()).hexdigest()
        if got == wanted:
            print("already here and verified:", target)
            return 0
        print("present but the checksum does not match -- fetching again")

    url = spec["source"]["download"]
    print("fetching", url)
    raw = urllib.request.urlopen(url, timeout=300).read()
    print(f"  {len(raw):,} bytes")

    archive = os.path.join(HERE, "_bundle.zip")
    open(archive, "wb").write(raw)
    if not zipfile.is_zipfile(archive):
        os.unlink(archive)
        print("that was not a zip. The blender.org link is an interstitial; "
              "the manifest's download URL is the mirror.", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with zipfile.ZipFile(archive) as zip_file:
        for item in zip_file.infolist():
            if item.filename.endswith(".blend"):
                with zip_file.open(item) as source, open(target, "wb") as out:
                    out.write(source.read())
    os.unlink(archive)

    got = hashlib.sha256(open(target, "rb").read()).hexdigest()
    if got != wanted:
        print(f"checksum mismatch\n  wanted {wanted}\n  got    {got}", file=sys.stderr)
        return 3

    print("verified:", target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
