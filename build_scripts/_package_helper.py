import zipfile, pathlib, sys

dist = pathlib.Path(sys.argv[1])
out  = pathlib.Path(sys.argv[2])

files = sorted(dist.rglob('*'))
total = sum(1 for f in files if f.is_file())
done  = 0

with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for path in files:
        if path.is_file():
            arcname = path.relative_to(dist.parent)
            zf.write(path, arcname)
            done += 1
            print(f'  [{done}/{total}] {arcname}')

print()
size = out.stat().st_size / 1024 / 1024
print(f'[OK] Bot.zip created ({size:.2f} MB) -> {out}')
