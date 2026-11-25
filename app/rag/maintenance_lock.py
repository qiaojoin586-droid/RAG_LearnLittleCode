"""Exclusive ownership of this workspace's embedded vector stores."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]


class VectorStoreBusy(RuntimeError):
    pass


def local_vector_owner(workspace=WORKSPACE):
    return _owner(Path(workspace) / '.cache' / 'vector-store-owner.lock')


def service_store_owner(directory, workspace=WORKSPACE):
    key = hashlib.sha256(os.path.normcase(str(Path(directory).resolve())).encode()).hexdigest()
    return _owner(Path(workspace) / '.cache' / f'chroma-service-{key}.lock')


@contextmanager
def _owner(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise VectorStoreBusy('Embedded vector storage is in use by another local process') from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise VectorStoreBusy('Embedded vector storage is in use by another local process') from error
        # Closing the file releases the lock, including when the process crashes.
        yield
