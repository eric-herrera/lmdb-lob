import time
import atexit
import sys

class Stats:
    instances = []
    def __init__(self, types=[]):
        self.__class__.instances.append(self)
        self.types = set(types)
        self._stats = {}

    def set(self, name, elapsed=0, count=1):
        self.types.add(name)
        cnt = name+'_cnt'
        avg = name+'_avg'
        tot = name+'_tot'
        for x in (cnt, tot):
            if x not in self._stats:
                self._stats[x] = 0
        self._stats[cnt] += count
        self._stats[tot] += elapsed

        if avg in self._stats:
            self._stats[avg] = (self._stats[avg] + elapsed) / 2
        else:
            self._stats[avg] = elapsed

    def text(self):
        out = []
        out.append("%-20s %10s %13s %13s %10s" % (
            'Name','Count','Total','Avg','Ops/sec'))
        for name in sorted(self.types):
            cnt = self._stats[name+'_cnt']
            avg = self._stats[name+'_avg']
            tot = self._stats[name+'_tot']
            ops = 0
            if tot > 0:
                ops = cnt / tot
            out.append("%-20.20s %10d %10.2f ms %10.2f ms %10d" % (
                name, cnt, tot * 1000, avg * 1000, ops
            ))


        return '\n'.join(out)

    def print_stats(self):
        print(self.text())

    def timeit(self, method):
        def timed(*args, **kw):
            ts = time.time()
            result = method(*args, **kw)
            te = time.time()
            self.set(method.__name__, (te - ts)) # * 1000
            return result
        return timed


def dumpstats():
    for x, i in enumerate(Stats.instances):
        print('*'*78)
        print('Stats for instance',x)
        print(i.text())

atexit.register(dumpstats)
