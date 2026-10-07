"""Use torch's OpenMP runtime for the VC environment's bundled libraries."""
import os
import sys
from pathlib import Path


def dedupe_libomp(python_path) -> list[str]:
    """Point bundled libomp copies at torch's one; return relative paths that were relinked."""
    if sys.platform == "win32":
        return []
    venv = Path(python_path).parent.parent
    relinked = []
    for site in sorted(venv.glob('lib/python3*/site-packages')):
        canonical = site / 'torch' / 'lib' / 'libomp.dylib'
        if not canonical.is_file():
            continue
        for path in sorted(site.glob('*/.dylibs/libomp.dylib')):
            if path.is_symlink() and path.resolve() == canonical.resolve():
                continue
            if not path.exists() and not path.is_symlink():
                continue
            tmp = path.with_name('libomp.dylib.ai-voice-tmp')
            if tmp.exists() or tmp.is_symlink():
                tmp.unlink()
            tmp.symlink_to(os.path.relpath(canonical, path.parent))
            os.replace(tmp, path)
            relinked.append(str(path.relative_to(site)))
    return relinked
