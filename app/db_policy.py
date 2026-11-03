"""Fail-closed external database write boundary for B2 preacceptance."""
import os
from pathlib import Path


class DatabaseBoundaryError(RuntimeError):
    pass


def checkout_roots():
    root = Path(__file__).resolve().parents[1]
    roots = [root]
    git = root / '.git'
    if git.is_file():
        gitdir = Path(git.read_text().strip().removeprefix('gitdir: ').strip())
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        common = gitdir / 'commondir'
        if common.is_file():
            roots.append((gitdir / common.read_text().strip()).resolve().parent)
    return tuple(p.resolve() for p in roots)


def safe_target(path):
    configured = os.environ.get('TECHDOCS_DATA_ROOT')
    if not configured or not Path(configured).is_absolute():
        raise DatabaseBoundaryError('Explicit absolute TECHDOCS_DATA_ROOT required')
    raw, root_raw = Path(path), Path(configured)
    for parent in (raw, *raw.parents, root_raw, *root_raw.parents):
        if parent.is_symlink() or parent.is_junction():
            raise DatabaseBoundaryError('Linked write path forbidden')
    target, root = raw.resolve(), root_raw.resolve()
    if target == root or not target.is_relative_to(root):
        raise DatabaseBoundaryError('Write target outside external data root')
    for checkout in checkout_roots():
        if root.is_relative_to(checkout) or target.is_relative_to(checkout):
            raise DatabaseBoundaryError('Source checkout writes forbidden')
    if target.exists() and target.stat().st_nlink != 1:
        raise DatabaseBoundaryError('Hard-linked write target forbidden')
    for name in os.environ.get('TECHDOCS_PROTECTED_DATABASES', '').split(os.pathsep):
        if name:
            source = Path(name).resolve()
            if target == source or (target.exists() and source.exists() and target.samefile(source)):
                raise DatabaseBoundaryError('Protected source/backup write forbidden')
    return target
