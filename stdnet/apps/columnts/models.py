'''A Redis timeseries module base on redis time-series and
redis strings.
'''
from stdnet import orm, SessionNotAvailable
from stdnet.lib import skiplist
from stdnet.utils import encoders, iteritems, zip

from .encoders import ValueEncoder

__all__ = ['TimeseriesCache', 'ColumnTS', 'ColumnTSField']

class TimeseriesCache(object):
    cache = None
    
    def __init__(self):
        self.merged_series = None
        self.fields = {}
        self.delete_fields = set()
        self.deleted_timestamps = set()

    def add(self, timestamp, field, value):
        if field not in self.fields:
            self.fields[field] = skiplist()
        self.fields[field].insert(timestamp,value)
        
    def clear(self):
        self.merged_series = None
        self.fields.clear()
        self.delete_fields.clear()
        self.deleted_timestamps.clear()


class ColumnTS(orm.TS):
    cache_class = TimeseriesCache
    pickler = encoders.DateTimeConverter()
    value_pickler = ValueEncoder()
    
    def front(self, *fields):
        '''Return the front pair of the structure'''
        v,f = tuple(self.irange(0, 0, fields = fields))
        if v:
            return (v[0],dict(((field,f[field][0]) for field in f)))
    
    def back(self, *fields):
        '''Return the back pair of the structure'''
        v,f = tuple(self.irange(-1, -1, fields = fields))
        if v:
            return (v[0],dict(((field,f[field][0]) for field in f)))
    
    def fields(self):
        '''Return a tuple of ordered fields for this :class:`ColumnTS`.'''
        return self.backend_structure().fields()
    
    def numfields(self):
        '''Number of fields'''
        return self.backend_structure().numfields()
    
    @orm.commit_when_no_transaction
    def add(self, dt, *args):
        self._add(dt, *args)
        
    @orm.commit_when_no_transaction
    def update(self, mapping):
        if isinstance(mapping,dict):
            mapping = iteritems(mapping)
        add = self._add
        for dt,v in mapping:
            add(dt,v)
    
    def irange(self, start = 0, end = -1, fields = None, novalues = False,
               callback = None):
        res = self.backend_structure().irange(start, end, fields, novalues)
        return self.async_handle(res, callback or self.load_data)
    
    def irange_and_delete(self):
        res = self.backend_structure().irange(0, -1, delete = True)
        return self.async_handle(res, self.load_data)
    
    def range(self, start, end, fields=None, novalues = False, callback=None):
        start = self.pickler.dumps(start)
        end = self.pickler.dumps(end)
        res = self.backend_structure().range(start, end, fields, novalues)
        return self.async_handle(res, callback or self.load_data)
    
    def get(self, dt, *fields):
        return self.range(dt, dt, fields, self._get)
        
    def istats(self, start=0, end=-1, fields=None):
        res = self.backend_structure().istats(start, end, fields)
        return self.async_handle(res, self._stats)
    
    def stats(self, start, end, fields=None):
        start = self.pickler.dumps(start)
        end = self.pickler.dumps(end)
        res = self.backend_structure().stats(start, end, fields)
        return self.async_handle(res, self._stats)
    
    def merge(self, *series, **kwargs):
        '''Merge this :class:`ColumnTS` with several other.
        
:parameter series: a tuple of two or three elements tuple.
    For example::
    
        (5, ts1),(-2, ts2)
        
:parameter kwargs: key-valued parameters for the merge.
:rtype: a new :class:`ColumnTS`
'''
        session = self.session
        for serie in series:
            if len(serie) < 2:
                raise ValueError('merge requires tuples of length 2 or more')
            for s in serie[1:]:
                if session is None:
                    session = s.session
        if not session:
            raise SessionNotAvailable('No session available')
        self.session = session
        fields = kwargs.get('fields') or ()
        self.backend_structure().merge(series, fields)
        session.add(self)
        
    @classmethod 
    def merged_series(cls, *series, **kwargs):
        '''Merge series into one timeseries.
        
:parameters series: a list of tuples where the nth element is a tuple
    of the form::

    (wight_n,ts_n1,ts_n2,..,ts_nMn)

The result will be calculated using the formula::

    ts = weight_1*ts_11*ts_12*...*ts_1M1 + weight_2*ts_21*ts_22*...*ts_2M2 +
         ...
'''
        kwargs['store'] = False
        target = cls()
        target.merge(*series, **kwargs)
        return target.irange_and_delete()
        
    # INTERNALS
    
    def load_data(self, result):
        loads = self.pickler.loads
        vloads = self.value_pickler.loads
        dt = [loads(t) for t in result[0]]
        vals = {}
        for f,data in result[1]:
            vals[f] = [vloads(d) for d in data]
        return (dt,vals)
    
    def _get(self, result):
        dt, fields = self.load_data(result)
        if dt:
            if len(fields) == 1:
                return tuple(fields.values())[0]
            else:
                return dict(((f,fields[f][0]) for f in fields))
    
    def _stats(self, result):
        if result:
            result['start'] = self.pickler.loads(result['start'])
            result['stop'] = self.pickler.loads(result['stop'])
        return result
    
    def _add(self, dt, *args):
        timestamp = self.pickler.dumps(dt)
        add = self.cache.add
        dump = self.value_pickler.dumps
        if len(args) == 1:
            mapping = args[0]
            if isinstance(mapping,dict):
                mapping = iteritems(mapping)
            for field,value in mapping:
                add(timestamp, field, dump(value))
        elif len(args) == 2:
            add(timestamp, args[0], dump(args[1]))
        else:
            raise TypeError('Expected a mapping or a field value pair')
        
    
class ColumnTSField(orm.StructureField):
    '''An experimenta timeseries field.'''
    
    def structure_class(self):
        return ColumnTS
    
