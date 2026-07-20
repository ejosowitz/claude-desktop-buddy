#!/usr/bin/env python3
"""
Import a pet from the OpenPets gallery (https://openpets.dev) as a character
pack and (optionally) flash it to the device — the whole pipeline in one shot:

  resolve name/slug/URL  ->  download spritesheet  ->  slice into our 7 states
  ->  prep (downscale + consistent crop)  ->  optional USB flash

OpenPets gallery pets ship a single 1536x1872 spritesheet (8 cols x 9 rows,
192x208 per frame) with a fixed row->animation layout and no per-pet layout
metadata — every pet uses the same grid — so we can slice any of them. We map
that layout onto this project's seven states.

Usage:
  python3 tools/import_openpet.py pinchy
  python3 tools/import_openpet.py https://openpets.dev/pets/pinchy-a4ca4e12
  python3 tools/import_openpet.py pinchy --flash

License: OpenPets is MIT. Keep attribution to the original artist — this tool
writes a README.md with the pet's gallery URL into the pack.
"""
import argparse, io, json, sys, tempfile, urllib.request
from pathlib import Path

from PIL import Image

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
import prep_character   # reuse install() for downscale + cross-state crop
import flash_character  # reuse flash() for USB uploadfs

CATALOG = "https://openpets.dev/pets/catalog.v3.json"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko)"}

# Canonical OpenPets grid (from the desktop app's defaultPetSprite).
COLS, ROWS = 8, 9

# our_state -> (row, max_frames, total_duration_ms)
# rows we skip: 1/2 (running left/right) — no equivalent state here.
STATE_MAP = {
    "sleep":     (5, 8, 3200),   # "failed": lies down, eyes shut
    "idle":      (0, 6, 4200),   # neutral
    "busy":      (7, 6, 1800),   # "running": active work
    "attention": (6, 6, 1600),   # "waiting": blocked / awaiting approval
    "celebrate": (4, 5, 1400),   # "jumping": success
    "dizzy":     (8, 6, 1600),   # "review": head-scratch / thinking
    "heart":     (3, 4, 1300),   # "waving": friendly greeting
}


def _get(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30).read()


def resolve(query: str) -> dict:
    """Find the catalog entry for a name, slug, or gallery URL."""
    q = query.strip().rstrip("/")
    slug = q.rsplit("/", 1)[-1] if "openpets.dev" in q or "/" in q else None
    key = (slug or q).lower()

    cat = json.loads(_get(CATALOG))
    matches = []
    for page_url in cat["pages"]:
        for pet in json.loads(_get(page_url)).get("pets", []):
            urls = f"{pet.get('zip','')} {pet.get('spritesheet','')} {pet.get('thumbnail','')}"
            if pet["id"].lower() == key or (slug and slug in urls) or key in urls:
                matches.append(pet)
    if not matches:
        sys.exit(f"no OpenPets pet matches '{query}'")
    if len(matches) > 1 and not slug:
        ids = ", ".join(sorted({m['id'] for m in matches}))
        sys.exit(f"'{query}' is ambiguous — matches: {ids}\n"
                 f"  disambiguate with the gallery URL (…/pets/<slug>)")
    return matches[0]


def _body_color(frames: list[Image.Image]) -> str:
    """Most common opaque color across frames, quantized — the pet's body hue."""
    from collections import Counter
    c = Counter()
    for f in frames:
        small = f.resize((48, 52))
        px = small.load()
        for y in range(small.height):
            for x in range(small.width):
                r, g, b, a = px[x, y]
                if a > 200:
                    c[(r & 0xE0, g & 0xE0, b & 0xE0)] += 1
    if not c:
        return "#FFFFFF"
    r, g, b = c.most_common(1)[0][0]
    return f"#{r:02X}{g:02X}{b:02X}"


def write_readme(dst: Path, disp: str, url: str) -> None:
    (dst / "README.md").write_text(
        f"# {disp}\n\nImported from the OpenPets gallery (MIT): {url}\n\n"
        f"Sliced from the pet's 8x9 spritesheet by `tools/import_openpet.py` and "
        f"mapped onto this project's seven states. Art copyright remains with the "
        f"original OpenPets artist; keep this attribution.\n")


def slice_to_pack(sheet: Image.Image, name: str, disp: str, url: str, dst: Path) -> None:
    w, h = sheet.size
    fw, fh = w // COLS, h // ROWS
    if (w, h) != (1536, 1872):
        print(f"  note: {w}x{h} is nonstandard — deriving {fw}x{fh} frames from the 8x9 grid")

    dst.mkdir(parents=True, exist_ok=True)
    idle_frames, states = [], {}
    for state, (row, nmax, total_ms) in STATE_MAP.items():
        frames = []
        for col in range(nmax):
            fr = sheet.crop((col * fw, row * fh, col * fw + fw, row * fh + fh))
            frames.append(fr)
        # drop trailing fully-transparent cells (pets vary in frames/row)
        while len(frames) > 1 and not frames[-1].getbbox():
            frames.pop()
        if state == "idle":
            idle_frames = frames
        per = max(60, round(total_ms / len(frames)))
        gif = dst / f"{state}.gif"
        frames[0].save(gif, save_all=True, append_images=frames[1:],
                       duration=per, loop=0, disposal=2)
        states[state] = f"{state}.gif"
        print(f"  {state:10s} row{row} {len(frames)} frames @ {per}ms")

    (dst / "manifest.json").write_text(json.dumps({
        "name": name,
        "colors": {"body": _body_color(idle_frames), "bg": "#000000",
                   "text": "#FFFFFF", "textDim": "#808080", "ink": "#000000"},
        "states": states,
    }, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Import an OpenPets gallery pet as a character pack.")
    ap.add_argument("pet", help="pet name, slug, or openpets.dev gallery URL")
    ap.add_argument("--flash", action="store_true", help="flash to the device over USB after prepping")
    ap.add_argument("--name", help="override the pack name (default: the pet's gallery id)")
    args = ap.parse_args()

    entry = resolve(args.pet)
    name = (args.name or entry["id"]).lower()
    name = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name)
    disp = entry.get("displayName", name)
    gallery_url = f"https://openpets.dev/pets/{entry['spritesheet'].split('/pets/')[1].split('/')[0]}"
    print(f"resolved '{args.pet}' -> {disp}  ({gallery_url})")

    sheet = Image.open(io.BytesIO(_get(entry["spritesheet"]))).convert("RGBA")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / name
        print("slicing spritesheet:")
        slice_to_pack(sheet, name, disp, gallery_url, src)
        print("\nprepping (downscale + consistent crop):")
        prep_character.install(src)   # writes characters/<name>/

    out = prep_character.OUT_ROOT / name
    write_readme(out, disp, gallery_url)   # prep rewrites the dir, so add attribution after
    if args.flash:
        print("\nflashing to device:")
        flash_character.flash(out)
    else:
        print(f"\ndone -> {out}")
        print(f"flash it:  python3 tools/flash_character.py characters/{name}")


if __name__ == "__main__":
    main()
