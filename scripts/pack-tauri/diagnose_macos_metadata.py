"""Temporary CI probe: record metadata names, never attribute values."""
import ctypes
from functools import lru_cache
import json
import os
from pathlib import Path
import struct
import sys
import zipfile


@lru_cache(maxsize=1)
def native_xattr():
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    libc.listxattr.restype = ctypes.c_ssize_t
    libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    libc.getxattr.restype = ctypes.c_ssize_t
    return libc


def attributes(path):
    libc = native_xattr()
    encoded = os.fsencode(path)
    size = libc.listxattr(encoded, None, 0, 1)
    if size < 0:
        raise OSError(ctypes.get_errno(), 'listxattr failed')
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    used = libc.listxattr(encoded, buffer, size, 1)
    if used < 0:
        raise OSError(ctypes.get_errno(), 'listxattr failed')
    result = {}
    for name in buffer.raw[:used].split(b'\0'):
        if name:
            length = libc.getxattr(encoded, name, None, 0, 0, 1)
            if length < 0:
                raise OSError(ctypes.get_errno(), 'getxattr failed')
            result[os.fsdecode(name)] = length
    return result


def snapshot(root):
    records = []
    errors = []
    paths = [root]
    for directory, dirs, files in os.walk(root, followlinks=False):
        paths.extend(Path(directory) / name for name in dirs + files)
    for path in paths:
        try:
            attrs = attributes(path)
            if attrs:
                records.append({'path': str(path.relative_to(root)), 'attributes': attrs})
        except OSError as exc:
            errors.append({'path': str(path.relative_to(root)), 'error': str(exc)})
    return {'root': str(root), 'scanned': len(paths), 'files': records, 'errors': errors}


def appledouble(raw):
    if len(raw) < 26 or raw[:4] != b'\x00\x05\x16\x07':
        raise ValueError('Not AppleDouble')
    entries = []
    attrs = {}
    for index in range(struct.unpack_from('>H', raw, 24)[0]):
        kind, offset, size = struct.unpack_from('>III', raw, 26 + index * 12)
        entries.append({'id': kind, 'size': size})
        if kind != 9:
            continue
        pos = (offset + 32 + 3) & ~3  # ATTR follows aligned FinderInfo.
        if raw[pos:pos + 4] != b'ATTR':
            continue
        count = struct.unpack_from('>8I2H', raw, pos)[-1]
        cursor = pos + 36
        for _ in range(count):
            _, length, _, name_length = struct.unpack_from('>IIHB', raw, cursor)
            name = raw[cursor + 11:cursor + 11 + name_length].rstrip(b'\0').decode('utf-8', 'replace')
            attrs[name] = length
            cursor = (cursor + 11 + name_length + 3) & ~3
    return {'entries': entries, 'attributes': attrs}


def archive_report(path):
    records = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.startswith('__MACOSX/'):
                continue
            record = {'member': info.filename, 'size': info.file_size}
            member = Path(info.filename.removeprefix('__MACOSX/'))
            if member.name.startswith('._') and not info.is_dir():
                record['source'] = str(member.with_name(member.name[2:]))
                try:
                    if info.file_size > 16 * 1024 * 1024:
                        raise ValueError('Metadata exceeds diagnostic read limit')
                    record.update(appledouble(archive.read(info)))
                except (ValueError, struct.error) as exc:
                    record['error'] = str(exc)
            records.append(record)
        return {'archive': str(path), 'total_entries': len(archive.infolist()), 'metadata': records}


def main():
    mode, label, source = sys.argv[1:]
    report = archive_report(Path(source)) if mode == 'zip' else snapshot(Path(source))
    target = Path(os.environ['MACOS_METADATA_DIAGNOSTICS'])
    target.mkdir(parents=True, exist_ok=True)
    (target / f'{label}.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f'Metadata diagnostic: {label} -> {target / (label + ".json")}')


if __name__ == '__main__':
    main()
