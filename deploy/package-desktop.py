from pathlib import Path
import hashlib
import json
import zipfile
import tarfile

root=Path.cwd()
out=root/"dist"
frontend=root/"evidence/CANDIDATE_IDENTITY.json"
known=json.loads(frontend.read_text(encoding="utf-8"))
for name, digest in known["source_files"].items():
    assert hashlib.sha256((root/name).read_bytes()).hexdigest()==digest, name

essential=[root/"README.md", root/"Dockerfile",root/".dockerignore"]
essential.extend(p for p in (root/"retrofit").rglob("*") if p.is_file() and "__pycache__" not in p.parts)
essential.extend(p for p in (root/"deploy").glob("*") if p.is_file() and p.name!="review-starter.py")
essential.extend([root/"tests/pdf_fixture.py",root/"evidence/factory-verification.json",root/"evidence/CANDIDATE_IDENTITY.json"])
essential.extend(root / name for name in (
    "evidence/distribution-verification.md",
    "examples/school-energy-sample.pdf",
    "examples/sample-draft.md",
    "web/workbench.ts",
))
essential.extend(path for path in (root / "docs").rglob("*") if path.is_file())
desktop={ "amd64": ["Start Retrofit Evidence.command","Stop Retrofit Evidence.command","Start Retrofit Evidence.cmd","Stop Retrofit Evidence.cmd","Start-RetrofitEvidence.ps1","start-linux.sh","stop-linux.sh"],
          "arm64": ["Start Retrofit Evidence.command","Stop Retrofit Evidence.command","Start Retrofit Evidence.cmd","Stop Retrofit Evidence.cmd","Start-RetrofitEvidence.ps1","start-linux.sh","stop-linux.sh"]}
for arch, launchers in desktop.items():
    image=out/f"retrofit-evidence-linux-{arch}.tar.gz"
    assert image.is_file()
    with tarfile.open(image,"r:gz") as packed:
        assert "manifest.json" in packed.getnames()
        manifest=json.load(packed.extractfile("manifest.json"))
        assert manifest[0]["RepoTags"]==["retrofit-evidence:0.2.0"],manifest
    bundle=out/f"RetrofitEvidence-0.2.0-{arch}.zip"
    with zipfile.ZipFile(bundle,"w",zipfile.ZIP_DEFLATED) as archive:
        for file in essential:
            rel=file.relative_to(root).as_posix()
            info=zipfile.ZipInfo.from_file(file,rel)
            mode=0o755 if file.suffix in {".command",".sh"} else 0o644
            info.create_system=3
            info.external_attr=mode<<16
            archive.writestr(info,file.read_bytes(),compress_type=zipfile.ZIP_DEFLATED)
        for name in launchers:
            info=zipfile.ZipInfo(name)
            info.create_system=3
            info.external_attr=(0o755 if name.endswith((".command",".sh")) else 0o644)<<16
            archive.writestr(info,(root/"platform"/name).read_bytes(),compress_type=zipfile.ZIP_DEFLATED)
        archive.write(image,image.name,compress_type=zipfile.ZIP_STORED)
    with zipfile.ZipFile(bundle) as archive:
        assert archive.testzip() is None
        assert archive.getinfo("Start Retrofit Evidence.command").external_attr >>16 == 0o755
    with bundle.open("rb") as handle:
        print(f"{hashlib.file_digest(handle,'sha256').hexdigest()}  {bundle.name}")
