"""Select and validate source files for a public, history-free distribution."""

from pathlib import Path
import re
import subprocess
import tarfile

PRIVATE_PARTS = {
    '.git',
    '.state',
    '.cache',
    '.tools',
    '.venv',
    'backups',
    'evidence',
    '__pycache__',
    'dist',
}
LEGACY_NAME = re.compile('super' + r'[ _-]*pipe|سوپر[ ‌_-]*' + 'پایپ', re.IGNORECASE)


def source_files(root):
    root = Path(root).resolve()
    result = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
        check=True,
        capture_output=True,
    )
    files = []
    for name in sorted(set(result.stdout.split(b'\0')) - {b''}):
        relative = Path(name.decode())
        path = root / relative
        if (
            relative.parent == Path('vendor')
            and relative.name.startswith('pgo-')
            and relative.suffix == '.tgz'
        ):
            raise ValueError(
                'The Crunchy packaged chart must be fetched from upstream, not redistributed'
            )
        if not path.exists() and not path.is_symlink():
            continue  # Deleted files must not return in an export.
        if (
            PRIVATE_PARTS.intersection(relative.parts)
            or relative.name.startswith('.env')
            or relative.suffix in {'.key', '.pem', '.kubeconfig', '.pyc'}
        ):
            raise ValueError(f'Private file selected for publication: {relative}')
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Publication requires regular files: {relative}')
        if LEGACY_NAME.search(str(relative)):
            raise ValueError(f'Legacy name in source path: {relative}')
        if path.suffix == '.tgz':
            with tarfile.open(path) as archive:
                for entry in archive:
                    if (
                        entry.name.startswith('/')
                        or '..' in Path(entry.name).parts
                        or entry.issym()
                        or entry.islnk()
                    ):
                        raise ValueError(f'Unsafe vendor archive: {relative}')
                    payload = archive.extractfile(entry).read() if entry.isfile() else b''
                    if LEGACY_NAME.search(entry.name) or LEGACY_NAME.search(
                        payload.decode('utf-8', errors='ignore')
                    ):
                        raise ValueError(f'Legacy name in vendor archive: {relative}')
        elif LEGACY_NAME.search(path.read_text(encoding='utf-8', errors='ignore')):
            raise ValueError(f'Legacy name in source: {relative}')
        files.append(relative)
    return files


def export_source(root, destination):
    root = Path(root).resolve()
    files = source_files(root)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    try:
        with tarfile.open(temporary, 'w:gz') as archive:
            for relative in files:
                info = archive.gettarinfo(
                    str(root / relative), arcname=str(Path('odoo-ha') / relative)
                )
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = 0
                info.mode = 0o755 if info.mode & 0o111 else 0o644
                with (root / relative).open('rb') as stream:
                    archive.addfile(info, stream)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return len(files)
