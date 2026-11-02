#!/usr/bin/env python3
"""Offline manifest-consistent backups; never overwrite a restore destination."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import fcntl
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import shutil


def backup(data, destination):
    data=Path(data).resolve(); destination=Path(destination).resolve()
    if not data.is_dir(): raise ValueError('Data directory not found')
    if destination.is_relative_to(data): raise ValueError('Backup destination must be outside data directory')
    destination.mkdir(parents=True,exist_ok=True)
    with (data/'.runtime.lock').open('a+') as lock:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('Stop V2 before backing up')
        with tempfile.TemporaryDirectory() as tmp:
            stage=Path(tmp)/'data'; stage.mkdir()
            for source in data.rglob('*'):
                if source.is_symlink(): raise ValueError('Symlinks in data directory are not supported')
                if not source.is_file() or source.name=='.runtime.lock' or source.name.endswith(('-wal','-shm')): continue
                target=stage/source.relative_to(data); target.parent.mkdir(parents=True,exist_ok=True)
                if source.suffix=='.sqlite3':
                    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst:
                        src.backup(dst)
                        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Backup integrity check failed')
                else: shutil.copy2(source,target)
            name=destination/('policy-v2-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'.tar.gz')
            with tarfile.open(name,'x:gz') as archive: archive.add(stage,arcname='data')
            name.chmod(0o600); return name


def restore(archive, destination):
    destination=Path(destination).resolve()
    if destination.exists(): raise ValueError('Restore destination must not exist')
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        with tarfile.open(archive,'r:gz') as source:
            for member in source.getmembers():
                path=Path(member.name)
                if path.is_absolute() or '..' in path.parts or path.parts[0]!='data' or not (member.isfile() or member.isdir()): raise ValueError('Unsafe backup member')
            source.extractall(tmp,filter='data')
        for path in (Path(tmp)/'data').rglob('*.sqlite3'):
            with closing(sqlite3.connect(path)) as con:
                if con.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('Restore integrity failed')
        shutil.move(str(Path(tmp)/'data'),destination)
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='action',required=True)
    one=sub.add_parser('backup'); one.add_argument('--data',required=True); one.add_argument('--target',required=True)
    two=sub.add_parser('restore'); two.add_argument('--archive',required=True); two.add_argument('--target',required=True)
    args=parser.parse_args()
    print(backup(args.data,args.target) if args.action=='backup' else restore(args.archive,args.target))
