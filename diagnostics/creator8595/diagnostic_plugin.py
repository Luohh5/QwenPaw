"""Temporary diagnostic plugin; observes and rethrows original failures."""
import functools
import importlib.metadata as metadata
import inspect
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import traceback

_LOG = logging.getLogger('creator_issue_8595')
_NAMES = ('imageio-ffmpeg', 'pypdfium2', 'matplotlib', 'pandas', 'geopandas', 'trimesh', 'oss2', 'qwenpaw')

def emit(kind, **data):
    _LOG.warning('CREATOR_DIAGNOSTIC %s', json.dumps({'kind':kind, **data}, default=str))

def identity():
    values={}
    for name in _NAMES:
        try:
            d=metadata.distribution(name)
            values[name]={'version':d.version, 'path':str(getattr(d,'_path',''))}
        except metadata.PackageNotFoundError as exc:
            values[name]={'error':type(exc).__name__, 'name':getattr(exc,'name',None)}
    emit('environment', frozen=bool(getattr(sys,'frozen',False)), executable=sys.executable, python=sys.version, sys_path=sys.path, packages=values)

def failed(name, exc):
    emit('exception', stage=name, exception_type=type(exc).__module__+'.'+type(exc).__name__, name=getattr(exc,'name',None), reason_code=getattr(exc,'reason_code',None), message=str(exc), traceback=''.join(traceback.format_exception(type(exc),exc,exc.__traceback__)))

def observe(name, original):
    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def wrapped(*args, **kwargs):
            emit('begin',stage=name)
            try:
                result=await original(*args,**kwargs)
            except BaseException as exc:
                failed(name,exc)
                raise
            emit('end',stage=name, result_type=type(result).__name__, reason_code=getattr(result,'reason_code',None), reason=getattr(result,'reason',None))
            if name=='load_plugin_from_path' and getattr(getattr(result,'manifest',None),'id',None)=='qwenpaw-creator':
                identity()
                try:
                    import imageio_ffmpeg
                    binary=imageio_ffmpeg.get_ffmpeg_exe()
                    checked=subprocess.run([binary,'-version'],capture_output=True,text=True,timeout=30)
                    emit('ffmpeg',binary=binary,returncode=checked.returncode,version=checked.stdout.splitlines()[:1])
                except Exception as exc:
                    failed('ffmpeg',exc)
            return result
    else:
        @functools.wraps(original)
        def wrapped(*args,**kwargs):
            emit('begin',stage=name)
            try:
                result=original(*args,**kwargs)
            except BaseException as exc:
                failed(name,exc)
                raise
            emit('end',stage=name,result_type=type(result).__name__)
            return result
    return wrapped

class Plugin:
    def register(self,api):
        from qwenpaw.plugins.loader import PluginLoader
        self.saved={}
        for name in ('_ensure_requirements_installed','_prepare_python_dependencies','_install_requirements','_install_requirements_frozen','_dependency_environment','load_plugin_from_path'):
            method=getattr(PluginLoader,name,None)
            if method is not None:
                self.saved[name]=method
                setattr(PluginLoader,name,observe(name,method))
        identity()

plugin=Plugin()
