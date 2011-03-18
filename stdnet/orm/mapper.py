import copy
import logging

from stdnet import utils
from stdnet import getdb
from stdnet.utils.importer import import_module

from .query import Manager, UnregisteredManager
from .base import StdNetType


logger = logging.getLogger('stdnet.mapper')

__all__ = ['clearall',
           'register',
           'unregister',
           'model_iterator',
           'register_applications',
           'register_application_models',
           'Manager',
           'UnregisteredManager']


# lock used to synchronize the "mapper compile" step
_COMPILE_MUTEX = utils.threading.RLock()

    
def clearall(exclude = None):
    exclude = exclude or []
    for meta in _registry.values():
        if not meta.name in exclude:
            meta.cursor.clear()


def register(model, backend = None, keyprefix = None, timeout = None):
    '''Register a :class:`stdnet.orm.StdModel`
model with a :class:`stdnet.backends.BackendDataServer` data server.
    
:parameter model: a :class:`stdnet.orm.StdModel` class. Must be provided.
:parameter backend: a backend connection string. Default ``settings.DEFAULT_BACKEND``.
:parameter keyprefix: a string used to prefix all database keys related to the model.
                      If not provided it is calculated from the connection string.
                      Default ``None``.
:parameter timeout: timeout in seconds for keys persistence.
                    If not provided it is calculated from the connection string.
                    Default ``None``.
    
**Usage**
    
For Redis the syntax is the following::

    import orm
    
    orm.register(Author, 'redis://my.host.name:6379/?db=1')
    orm.register(Book, 'redis://my.host.name:6379/?db=2')
    orm.register(MyOtherModel, 'redis://my.host.name:6379/?db=2&keyprefix=differentprefix')
    
``my.host.name`` can be ``localhost`` or an ip address or a domain name,
while ``db`` indicates the database number (very useful for separating data
on the same redis instance).'''
    global _registry
    from stdnet.conf import settings
    backend = backend or settings.DEFAULT_BACKEND
    #prefix  = keyprefix or model._meta.keyprefix or settings.DEFAULT_KEYPREFIX or ''
    meta = model._meta
    objects = getattr(model,'objects',None)
    if objects is None or isinstance(objects,UnregisteredManager):
        objects = Manager()
    else:
        objects = copy.copy(objects)
    model.objects = objects
    meta.cursor = getdb(backend)
    params = meta.cursor.params
    meta.keyprefix = keyprefix if keyprefix is not None else params.get('prefix',settings.DEFAULT_KEYPREFIX)
    meta.timeout = timeout if timeout is not None else params.get('timeout',0)
    objects._setmodel(model)
    _registry[model] = meta
    return str(meta.cursor)


def unregister(model):
    global _registry 
    _registry.pop(model,None)
    model._meta.cursor = None
    
    
def model_iterator(application):
    if hasattr(application,'__iter__'):
        for app in application:
            for m in model_iterator(app):
                yield m
    else:
        mod = import_module(application)
        mod_name = mod.__name__
        try:
            mod_models = import_module(application+'.models')
        except:
            raise StopIteration
        
        for name in dir(mod_models):
            obj = getattr(mod_models,name)
            if isinstance(obj,StdNetType) and hasattr(obj,'_meta'):
                yield obj


def register_application_models(application,
                                models = None,
                                app_defaults=None,
                                default=None):
    '''Generator of which register models'''
    app_defaults = app_defaults or {}
    for obj in model_iterator(application):
        name = obj._meta.name
        if models and name not in models:
            continue
        name = str(obj._meta)
        if not name in app_defaults:
            name = obj._meta.app_label
        if name in app_defaults:
            args = app_defaults[name]
        else:
            args = default
        register(obj,args)
        yield obj


def register_applications(applications, **kwargs):
    '''Loop over applications and register models.
    '''
    models = []
    for app in applications:
        models.extend(register_application_models(app,**kwargs))
    return models



_registry = {}



class Mapper(object):
    
    def __init__(self,
                 class_,
                 local_table,
                 properties = None,
                 primary_key = None):
        self.class_ = class_
        self.local_table = local_table
        self.compiled = False
        
        _COMPILE_MUTEX.acquire()
        try:
            self._configure_inheritance()
            self._configure_extensions()
            self._configure_class_instrumentation()
            self._configure_properties()
            self._configure_pks()
            global _new_mappers
            _new_mappers = True
            self._log("constructed")
        finally:
            _COMPILE_MUTEX.release()
            