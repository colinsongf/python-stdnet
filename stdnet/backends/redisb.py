'''Redis backend implementation'''
from copy import copy
import json
from hashlib import sha1
from itertools import chain
from functools import partial
from collections import namedtuple

import stdnet
from stdnet import FieldValueError, CommitException, QuerySetError, on_result
from stdnet.utils import to_string, map, gen_unique_id, zip,\
                             native_str, flat_mapping, unique_tuple
from stdnet.lib import redis
from stdnet.backends import BackendStructure, query_result, session_result,\
                            instance_session_result, on_result, range_lookups

MIN_FLOAT =-1.e99

################################################################################
#    prefixes for data
OBJ = 'obj'     # the hash table for a instance
TMP = 'tmp'     # temorary key
ODM_SCRIPTS = ('odmrun', 'move2set', 'zdiffstore')
################################################################################

def pairs_to_dict(response, encoding):
    "Create a dict given a list of key/value pairs"
    it = iter(response)
    return dict(((k.decode(encoding), v) for k, v in zip(it, it)))

class odmrun(redis.RedisScript):
    script = (redis.read_lua_file('tabletools'),
              # timeseries must be included before utils
              redis.read_lua_file('commands.timeseries'),
              redis.read_lua_file('commands.utils'),
              redis.read_lua_file('odm'))
    required_scripts = ODM_SCRIPTS
        
    def callback(self, response, meta=None, backend=None, command=None, **opts):
        if command == 'delete':
            res = (instance_session_result(r,False,r,True,0) for r in response)
            return session_result(meta, res)
        elif command == 'commit':
            res = self._wrap_commit(response, **opts)
            return session_result(meta, res)
        elif command == 'load':
            return self.load_query(response, backend, meta, **opts)
        elif command == 'structure':
            return self.flush_structure(response, backend, meta, **opts)
        else:
            return response
        
    def _wrap_commit(self, response, iids=None, redis_client=None, **options):
        for id, iid in zip(response, iids):
            id, flag, info = id
            if int(flag):
                yield instance_session_result(iid, True, id, False, float(info))
            else:
                msg = info.decode(redis_client.encoding)
                yield CommitException(msg)
    
    def load_query(self, response, backend, meta, get=None, fields=None,
                   fields_attributes=None, redis_client=None, **options):
        if get:
            tpy = meta.dfields.get(get).to_python
            return [tpy(v, backend) for v in response]
        else:
            data, related = response
            encoding = redis_client.encoding
            data = self.build(data, meta, fields, fields_attributes, encoding)
            related_fields = {}
            if related:
                for fname, rdata, fields in related:
                    fname = native_str(fname, encoding)
                    fields = tuple(native_str(f, encoding) for f in fields)
                    related_fields[fname] =\
                        self.load_related(meta, fname, rdata, fields, encoding)
            return backend.objects_from_db(meta, data, related_fields)
    
    def build(self, response, meta, fields, fields_attributes, encoding):
        fields = tuple(fields) if fields else None
        if fields:
            if len(fields) == 1 and fields[0] in (meta.pkname(), ''):
                for id in response:
                    yield id, (), {}
            else:
                for id, fdata in response:
                    yield id, fields, dict(zip(fields_attributes, fdata))
        else:
            for id, fdata in response:
                yield id, None, pairs_to_dict(fdata, encoding)
                
    def load_related(self, meta, fname, data, fields, encoding):
        '''Parse data for related objects.'''
        field = meta.dfields[fname]
        if field in meta.multifields:
            fmeta = field.structure_class()._meta
            if fmeta.name in ('hashtable', 'zset'):
                return ((native_str(id, encoding),
                         pairs_to_dict(fdata, encoding)) for \
                        id, fdata in data)
            else:
                return ((native_str(id, encoding), fdata) for id, fdata in data)
        else:
            # this is data for stdmodel instances
            return self.build(data, meta, fields, fields, encoding)
    
    
class check_structures(redis.RedisScript):
    script = redis.read_lua_file('structures')
    
    def callback(self, response, meta=None, instances=None, **opts):
        results = []
        for type, instance in zip(response, instances):
            state = instance.state()
            if state.deleted:
                if state.persistent:
                    raise InvalidTransaction('Could not delete %s', instance)
            results.append(instance_session_result(
                        state.iid, instance, instance.id, state.deleted, 0))
        return session_result(meta, results)
    

################################################################################
##    REDIS QUERY CLASS
################################################################################
class RedisQuery(stdnet.BackendQuery):
    card = None
    _meta_info = None
    script_dep = {'script_dependency': ('build_query','move2set')}
    
    def zism(self, r):
        return r is not None
    
    def sism(self, r):
        return r
    
    @property
    def meta_info(self):
        if self._meta_info == None:
            self._meta_info = json.dumps(self.backend.meta(self.meta))
        return self._meta_info
    
    def _build(self, pipe=None, **kwargs):
        # Accumulate a query
        self.pipe = pipe if pipe is not None else self.backend.client.pipeline()
        qs = self.queryelem
        pipe = self.pipe
        backend = self.backend
        key, meta, keys, args = None, self.meta, [], []
        pkname = meta.pkname()
        for child in qs:
            if getattr(child, 'backend', None) == backend:
                lookup, value = 'set', child
            else:
                lookup, value = child
            if lookup == 'set':
                be = value.backend_query(pipe=pipe)
                keys.append(be.query_key)
                args.extend(('set', be.query_key))
            else:
                if isinstance(value, tuple):
                    value = self.dump_nested(*value)
                args.extend((lookup, '' if value is None else value))
        temp_key = True
        if qs.keyword == 'set':
            if qs.name == pkname and not args:
                key = backend.basekey(meta, 'id')
                temp_key = False
            else:
                key = backend.tempkey(meta)
                keys.insert(0, key)
                backend.odmrun(pipe, 'query', meta, keys, self.meta_info,
                               qs.name, *args)
        else:
            key = backend.tempkey(meta)
            p = 'z' if meta.ordering else 's'
            pipe.execute_script('move2set', keys, p)
            if qs.keyword == 'intersect':
                command = getattr(pipe, p+'interstore')
            elif qs.keyword == 'union':
                command = getattr(pipe, p+'unionstore')
            elif qs.keyword == 'diff':
                command = getattr(pipe, p+'diffstore')
            else:
                raise ValueError('Could not perform %s operation' % qs.keyword)
            command(key, keys)
        where = self.queryelem.data.get('where')
        if where:
            keys.insert(0, key)
            if not temp_key:
                temp_key = True
                key = backend.tempkey(meta)
            keys.insert(0, key)
            backend.where_run(pipe, self.meta_info, keys, *where)
        #
        # If we are getting a field (for a subsequent query maybe)
        # unwind the query and store the result
        gf = qs._get_field 
        if gf and gf != pkname:
            field_attribute = meta.dfields[gf].attname
            bkey = key
            if not temp_key:
                temp_key = True
                key = backend.tempkey(meta)
            okey = backend.basekey(meta, OBJ, '*->' + field_attribute)
            pipe.sort(bkey, by='nosort', get=okey, store=key)
            self.card = getattr(pipe, 'llen')
        if temp_key:
            pipe.expire(key, self.expire)
        self.query_key = key
    
    def _execute_query(self):
        '''Execute the query without fetching data. Returns the number of
elements in the query.'''
        pipe = self.pipe
        if not self.card:
            if self.meta.ordering:
                self.ismember = getattr(self.backend.client, 'zrank')
                self.card = getattr(pipe, 'zcard')
                self._check_member = self.zism
            else:
                self.ismember = getattr(self.backend.client, 'sismember')
                self.card = getattr(pipe, 'scard')
                self._check_member = self.sism
        else:
            self.ismember = None
        self.card(self.query_key)
        #self.pipe.add_callback(lambda processed, result :
        #                            query_result(self.query_key, result))
        #self.commands, result = redis_execution(self.pipe, query_result)
        return on_result(pipe.execute(), lambda r: r[-1])
    
    def order(self, last):
        '''Perform ordering with respect model fields.'''
        desc = last.desc
        field = last.name
        nested = last.nested
        nested_args = []
        while nested:
            meta = nested.model._meta
            nested_args.extend((self.backend.basekey(meta), nested.name))
            last = nested
            nested = nested.nested
        method = 'ALPHA' if last.field.internal_type == 'text' else ''
        if field == last.model._meta.pkname():
            field = ''
        return {'field': field,
                'method': method,
                'desc': desc,
                'nested': nested_args}
    
    def dump_nested(self, value, nested):
        nested_args = []
        if nested:
            for name, meta in nested:
                if meta:
                    meta = self.backend.basekey(meta)
                nested_args.extend((name, meta))
        return json.dumps((value, nested_args))
                
                
    def _has(self, val):
        r = self.ismember(self.query_key, val)
        return self._check_member(r)
    
    def get_redis_slice(self, slic):
        if slic:
            start = slic.start or 0
            stop = slic.stop
        else:
            start = 0
            stop = None
        return start,stop
    
    def _items(self, slic):
        # Unwind the database query by creating a list of arguments for
        # the load_query lua script
        backend = self.backend
        meta = self.meta
        name = ''
        order = ()
        start, stop = self.get_redis_slice(slic)
        if self.queryelem.ordering:
            order = self.order(self.queryelem.ordering)
        elif meta.ordering:
            name = 'DESC' if meta.ordering.desc else 'ASC'
        elif start or stop is not None:
            order = self.order(meta.get_sorting(meta.pkname()))
        # Wen using the sort algorithm redis requires the number of element
        # not the stop index
        if order:
            name = 'explicit'
            N = self.execute_query()
            if stop is None:
                stop = N
            elif stop < 0:
                stop += N
            if start < 0:
                start += N
            stop -= start
        elif stop is None:
            stop = -1
        get = self.queryelem._get_field
        fields_attributes = None
        pkname_tuple = (meta.pk.name,)
        # if the get_field is available, we only load that field
        if get:
            if slic:
                raise QuerySetError('Cannot slice a queryset in conjunction '
                                    'with get_field. Use load_only instead.')
            if get == meta.pk.name:
                fields_attributes = fields = pkname_tuple
            else:
                fields, fields_attributes = meta.backend_fields((get,))
        else:
            fields = self.queryelem.fields or None
            if fields:
                fields = unique_tuple(fields, self.queryelem.select_related or ())
            if fields == pkname_tuple:
                fields_attributes = fields
            elif fields:
                fields, fields_attributes = meta.backend_fields(fields)
            else:
                fields_attributes = ()
        options = {'ordering': name,
                   'order': order,
                   'start': start,
                   'stop': stop,
                   'fields': fields_attributes,
                   'related': dict(self.related_lua_args()),
                   'get': get}
        joptions = json.dumps(options)
        options.update({'fields': fields,
                        'fields_attributes': fields_attributes})
        return backend.odmrun(backend.client, 'load', meta, (self.query_key,),
                              self.meta_info, joptions, **options) 

    def related_lua_args(self):
        '''Generator of load_related arguments'''
        related = self.queryelem.select_related
        if related:
            meta = self.meta
            for rel in related:
                field = meta.dfields[rel]
                relmodel = field.relmodel
                bk = self.backend.basekey(relmodel._meta) if relmodel else ''
                fields = list(related[rel])
                if meta.pkname() in fields:
                    fields.remove(meta.pkname())
                    if not fields:
                        fields.append('')
                data = {'field': field.attname,
                        'type': field.type if field in meta.multifields else '',
                        'bk': bk,
                        'fields': fields}
                yield field.name, data
            

def iteretor_pipelined(f):
    
    def _(self):
        if self.is_pipeline:
            return iter(())
        else:
            return f(self)
        
    return _

################################################################################
##    STRUCTURES
################################################################################
class RedisStructure(BackendStructure):
        
    @property
    def is_pipeline(self):
        return self.client.is_pipeline
        
    def delete(self):
        return self.client.delete(self.id)
    
    
class String(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        data = cache.getvalue()
        if data:
            self.client.append(self.id, data)
            result = True
        return result
    
    def size(self):
        return self.client.strlen(self.id)
    
    def incr(self, num=1):
        return self.client.incr(self.id, num)
            
    
class Set(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            self.client.sadd(self.id, *cache.toadd)
            result = True
        if cache.toremove:
            self.client.srem(self.id, *cache.toremove)
            result = True
        return result
    
    def size(self):
        return self.client.scard(self.id)
    
    def __iter__(self):
        return self.client.smembers(self.id)
    

class Zset(RedisStructure):
    '''Redis ordered set structure'''
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            flat = cache.toadd.flat()
            self.client.zadd(self.id, *flat)
            result = True
        if cache.toremove:
            flat = tuple((el[1] for el in cache.toremove))
            self.client.zrem(self.id, *flat)
            result = True
        return result
    
    def get(self, score):
        r = self.range(score, score, withscores=False)
        if r:
            if len(r) > 1:
                return r
            else:
                return r[0]
    
    def items(self):
        return self.irange(withscores=True)
    
    def values(self):
        return self.irange(withscores=False)
    __iter__ = values
    
    def size(self):
        return self.client.zcard(self.id)
    
    def rank(self, value):
        return self.client.zrank(self.id, value)
        
    def count(self, start, stop):
        return self.client.zcount(self.id, start, stop)
    
    def range(self, start, end, desc=False, withscores=True, **options):
        return on_result(
                self.client.zrangebyscore(self.id, start, end, desc=desc,
                                          withscores=withscores, **options),
                partial(self._range, withscores))
    
    def irange(self, start=0, stop=-1, desc=False, withscores=True, **options):
        return on_result(
                self.client.zrange(self.id, start, stop, desc=desc,
                                   withscores=withscores, **options),
                partial(self._range, withscores))
    
    def ipop_range(self, start, stop=None, withscores=True, **options):
        '''Remove and return a range from the ordered set by rank (index).'''
        return on_result(
                self.client.zpopbyrank(self.id, start, stop,
                                       withscores=withscores, **options),
                partial(self._range, withscores))
        
    def pop_range(self, start, stop=None, withscores=True, **options):
        '''Remove and return a range from the ordered set by score.'''
        return on_result(
                self.client.zpopbyscore(self.id, start, stop,
                                        withscores=withscores, **options),
                partial(self._range, withscores))
            
    # PRIVATE
    def _range(self, withscores, result):
        if withscores:
            return ((score,v) for v,score in result)
        else:
            return result
        

class List(RedisStructure):
    
    def pop_front(self):
        return self.client.lpop(self.id)
    
    def pop_back(self):
        return self.client.rpop(self.id)

    def block_pop_front(self, timeout):
        return self.client.blpop(self.id, timeout)
    
    def block_pop_front(self, timeout):
        return self.client.brpop(self.id, timeout)
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.front:
            self.client.lpush(self.id, *cache.front)
            result = True
        if cache.back:
            self.client.rpush(self.id, *cache.back)
            result = True
        return result
    
    def size(self):
        return self.client.llen(self.id)
    
    def __iter__(self):
        return iter(self.client.lrange(self.id, 0, -1))


class Hash(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            self.client.hmset(self.id, cache.toadd)
            result = True
        if cache.toremove:
            self.client.hdel(self.id, *cache.toremove)
            result = True
        return result
        
    def size(self):
        return self.client.hlen(self.id)
    
    def get(self, key):
        return self.client.hget(self.id, key)
    
    def pop(self, key):
        pi = self.is_pipeline
        p = self.client if pi else self.client.pipeline()
        p.hget(self.id, key).hdel(self.id, key)
        if not pi:
            result = p.execute()
            if result[1]:
                return result[0]
    
    def remove(self, *fields):
        return self.client.hdel(self.id, *fields)
    
    def __contains__(self, key):
        return self.client.hexists(self.id, key)
    
    def __iter__(self):
        return iter(self.client.hkeys(self.id))
    
    @iteretor_pipelined
    def values(self):
        return iter(self.client.hvals(self.id))
    
    @iteretor_pipelined        
    def items(self):
        return iter(self.client.hgetall(self.id))
    
    
class TS(RedisStructure):
    '''Redis timeseries implementation is based on the ts.lua script'''
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.toadd:
            self.client.execute_script('ts_commands', self.id, 'add',
                                    *cache.toadd.flat())
            result = True
        if cache.toremove:
            raise NotImplementedError('Cannot remove. TSDEL not implemented')
        return result
    
    def __contains__(self, timestamp):
        return self.client.execute_script('ts_commands', self.id, 'exists',
                                       timestamp)
    
    def size(self):
        return self.client.execute_script('ts_commands', self.id, 'size')
    
    def count(self, start, stop):
        return self.client.execute_script('ts_commands', self.id, 'count',
                                       start, stop)
    
    def times(self, time_start, time_stop, **kwargs):
        return self.client.execute_script('ts_commands', self.id, 'times',
                                       time_start, time_stop, **kwargs)
            
    def itimes(self, start=0, stop=-1, **kwargs):
        return self.client.execute_script('ts_commands', self.id, 'itimes',
                                       start, stop, **kwargs)
    
    def get(self, dte):
        return self.client.execute_script('ts_commands', self.id, 'get', dte)
    
    def rank(self, dte):
        return self.client.execute_script('ts_commands', self.id, 'rank', dte)
    
    def pop(self, dte):
        return self.client.execute_script('ts_commands', self.id, 'pop', dte)
    
    def ipop(self, index):
        return self.client.execute_script('ts_commands', self.id, 'ipop', index)
            
    def range(self, time_start, time_stop, **kwargs):
        return self.client.execute_script('ts_commands', self.id, 'range',
                                       time_start, time_stop, **kwargs)
            
    def irange(self, start=0, stop=-1, **kwargs):
        return self.client.execute_script('ts_commands', self.id, 'irange',
                                       start, stop, **kwargs)
        
    def pop_range(self, time_start, time_stop, **kwargs):
        return self.client.execute_script('ts_commands', self.id, 'pop_range',
                                       time_start, time_stop, **kwargs)
            
    def ipop_range(self, start=0, stop=-1, **kwargs):
        return self.client.execute_script('ts_commands', self.id, 'ipop_range',
                                       start, stop, **kwargs)


class NumberArray(RedisStructure):
    
    def flush(self):
        cache = self.instance.cache
        result = None
        if cache.back:
            self.client.execute_script('numberarray_pushback', self.id,
                                    *cache.back)
            result = True
        return result
    
    def get(self, index):
        return self.client.execute_script('numberarray_getset', self.id,
                                       'get', index+1)
    
    def set(self, value):
        return self.client.execute_script('numberarray_getset', self.id,
                                       'set', index+1, value)
    
    def __iter__(self):
        return iter(self.client.execute_script('numberarray_all_raw', self.id))
    
    def resize(self, size, value = None):
        if value is not None:
            argv = (size,value)
        else:
            argv = (size,)
        return self.client.execute_script('numberarray_resize', self.id, *argv)
    
    def size(self):
        return self.client.strlen(self.id)//8
    

class ts_commands(redis.RedisScript):
    script = (redis.read_lua_file('commands.timeseries'),
              redis.read_lua_file('tabletools'),
              redis.read_lua_file('ts'))
    
    
class numberarray_resize(redis.RedisScript):
    script = (redis.read_lua_file('numberarray'),
              '''return array:new(KEYS[1]):resize(unpack(ARGV))''')
    
class numberarray_all_raw(redis.RedisScript):
    script = (redis.read_lua_file('numberarray'),
              '''return array:new(KEYS[1]):all_raw()''')
    
class numberarray_getset(redis.RedisScript):
    script = (redis.read_lua_file('numberarray'),
              '''local a = array:new(KEYS[1])
if ARGV[1] == 'get' then
    return a:get(ARGV[2],true)
else
    a:set(ARGV[2],ARGV[3],true)
end''')
    
class numberarray_pushback(redis.RedisScript):
    script = (redis.read_lua_file('numberarray'),
              '''local a = array:new(KEYS[1])
for _,v in ipairs(ARGV) do
    a:push_back(v,true)
end''')


################################################################################
##    REDIS CACHE
################################################################################
class CacheServer(stdnet.CacheServer):
    
    def __init__(self, client):
        self.client = client
        
    def set(self, key, value, timeout=None):
        return self.client.set(id, value, timeout or 0)
    
    def get(self, key, default=None):
        v = self.client.get(key)
        if v:
            return v
        else:
            return default

    def expire(self, key, timeout):
        return self.client.expire(key, timeout)
            
    def __contains__(self, key):
        return self.client.exists(key)


################################################################################
##    REDIS BACKEND
################################################################################
class BackendDataServer(stdnet.BackendDataServer):
    Query = RedisQuery
    _redis_clients = {}
    default_port = 6379
    struct_map = {'set': Set,
                  'list': List,
                  'zset': Zset,
                  'hashtable': Hash,
                  'ts': TS,
                  'numberarray': NumberArray,
                  'string': String}
        
    def setup_connection(self, address):
        if len(address) == 2:
            address = tuple(address)
        elif len(address) == 1:
            address = address[0]
        if 'db' not in self.params:
            self.params['db'] = 0
        rpy = redis.redis_client(address=address, **self.params)
        if self.namespace:
            self.params['namespace'] = self.namespace
        return rpy
    
    def auto_id_to_python(self, value):
        return int(value)
    
    def ping(self):
        return self.client.ping()
    
    def as_cache(self):
        if self.namespace:
            c = redis.PrefixedRedis(self.client, self.namespace)
        else:
            c = self.client
        return CacheServer(c)
        
    def disconnect(self):
        self.client.connection_pool.disconnect()
            
    def load_scripts(self, *names):
        if not names:
            names = redis.registered_scripts()
        pipe = self.client.pipeline()
        for name in names:
            script = redis.get_script(name)
            if script:
                pipe.script_load(script.script)
        return pipe.execute()
    
    def meta(self, meta):
        '''Extract model metadata for lua script stdnet/lib/lua/odm.lua'''
        data = meta.as_dict()
        data['namespace'] = self.basekey(meta)
        return data
    
    def odmrun(self, client, command, meta, keys, meta_info, *args, **options):
        options.update({'backend': self, 'meta': meta, 'command': command})
        return client.execute_script('odmrun', keys, command, meta_info, *args,
                                     **options)
        
    def where_run(self, client, meta_info, keys, where, load_only):
        where = redis.read_lua_file('where', context={'where_clause': where})
        numkeys = len(keys)
        keys.append(meta_info)
        if load_only:
            args.append(json.dumps(load_only))
        return client.eval(where, numkeys, keys_args)
        
    def execute_session(self, session):
        '''Execute a session in redis.'''
        pipe = self.client.pipeline()
        for sm in session:  #loop through model sessions
            meta = sm.meta
            model_type = meta.model._model_type
            if model_type == 'structure':
                self.flush_structure(sm, pipe)
            elif model_type == 'object':
                meta_info = json.dumps(self.meta(meta))
                delquery = sm.get_delete_query(pipe=pipe)
                self.accumulate_delete(pipe, delquery)
                dirty = tuple(sm.iterdirty())
                N = len(dirty)
                if N:
                    lua_data = [N]
                    processed = []
                    for instance in dirty:
                        state = instance.state()
                        if not instance.is_valid():
                            raise FieldValueError(
                                        json.dumps(instance._dbdata['errors']))
                        score = MIN_FLOAT
                        if meta.ordering:
                            if meta.ordering.auto:
                                score = meta.ordering.name.incrby 
                            else:
                                v = getattr(instance, meta.ordering.name, None)
                                if v is not None:
                                    score = meta.ordering.field.scorefun(v)
                        data = instance._dbdata['cleaned_data']
                        action = state.action
                        id = state.iid
                        if not state.persistent:
                            id = instance.pkvalue() or ''
                        data = flat_mapping(data)
                        lua_data.extend((action, id, score, len(data)))
                        lua_data.extend(data)
                        processed.append(state.iid)
                    self.odmrun(pipe, 'commit', meta, (), meta_info,
                                *lua_data, iids=processed)
        return pipe.execute()
    
    def accumulate_delete(self, pipe, backend_query):
        # Accumulate models queries for a delete. It loops through the
        # related models to build related queries.
        # We pass the pipe since the backend_query may have been evaluated
        # using a different pipe
        if backend_query is None:
            return
        query = backend_query.queryelem
        keys = (backend_query.query_key,)
        meta_info = backend_query.meta_info
        meta = query.meta
        rel_managers = []
        for name in meta.related:
            rmanager = getattr(meta.model, name)
            # the related manager model is the same as current model
            if rmanager.model == meta.model:
                self.odmrun(pipe, 'aggregate', meta, keys, meta_info,
                            rmanager.field.attname)
            else:
                rel_managers.append(rmanager)
        # loop over related managers
        for rmanager in rel_managers:
            # IMPORTANT. delete only if field is required
            if rmanager.field.required:
                rq = rmanager.query_from_query(query).backend_query(pipe=pipe)
                self.accumulate_delete(pipe, rq)
        self.odmrun(pipe, 'delete', meta, keys, meta_info)
    
    def tempkey(self, meta, name = None):
        return self.basekey(meta, TMP, name if name is not None else\
                                        gen_unique_id())
        
    def flush(self, meta=None):
        '''Flush all model keys from the database'''
        pattern = self.basekey(meta) if meta else self.namespace
        return self.client.delpattern('%s*' % pattern)
        
    def clean(self, meta):
        return self.client.delpattern(self.tempkey(meta, '*'))
            
    def model_keys(self, meta):
        pattern = '%s*' % self.basekey(meta)
        return self.client.keys(pattern)
        
    def instance_keys(self, obj):
        meta = obj._meta
        keys = [self.basekey(meta, OBJ, obj.id)]
        for field in meta.multifields:
            f = getattr(obj,field.attname)
            keys.append(f.id)
        return keys
    
    def flush_structure(self, sm, pipe):
        processed = []
        keys = []
        for instance in chain(sm._delete_query, sm.dirty):
            state = instance.state()
            binstance = instance.backend_structure(pipe)
            binstance.commit()
            keys.append(binstance.id)
            processed.append(instance)
        if processed:
            pipe.execute_script('check_structures', keys, meta=sm.meta,
                                instances=processed)
        
    def publish(self, channel, message):
        return self.client.execute_command('PUBLISH', channel, message)
        
    def subscriber(self, **kwargs):
        return redis.Subscriber(self.client, **kwargs)