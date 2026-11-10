"""Local append-only history and incremental probability assessment. No Live imports or RNG use.

Raw journals/pools are authoritative. health-state.json is a rebuildable checkpoint;
health.json is a compact human-readable report. No historic journals are removed.
"""
import collections
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

SCHEMA = 1
MAX_SHARD = 4 * 1024 * 1024
_ASSESSMENTS = {}


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def atomic(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(encoded(value) + '\n', encoding='ascii')
    os.replace(str(temporary), str(path))


class History:
    def __init__(self, library, version, warning=lambda message: None):
        self.root = Path(library) / 'Optimystery History'
        self.version, self.warning = version, warning
        self.session = 'companion-' + uuid.uuid4().hex
        self.sequence, self.part = 0, 0
        self.pool_cache = set()
        self.last_warning = None
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'pools').mkdir(exist_ok=True)
        # Multiple control-surface instances in one Live process share one reader/checkpoint.
        key = str(self.root.resolve())
        if key not in _ASSESSMENTS: _ASSESSMENTS[key] = Assessment(self.root)
        self.assessment = _ASSESSMENTS[key]

    def emit(self, event, data=None, roll=None):
        try:
            self.sequence += 1
            entry = dict(schema=SCHEMA, event_id=self.session + ':' + str(self.sequence),
                         session=self.session, producer='companion', version=self.version,
                         time_ms=int(time.time()*1000), event=event, roll=roll, data=data or {})
            path = self.root / ('%s-%d.jsonl' % (self.session, self.part))
            if path.exists() and path.stat().st_size >= MAX_SHARD:
                self.part += 1
                path = self.root / ('%s-%d.jsonl' % (self.session, self.part))
            with path.open('a', encoding='ascii') as stream:
                stream.write(encoded(entry) + '\n')
            self.last_warning = None
        except Exception as exc:
            self.warn(exc)

    def warn(self, exc):
        message = str(exc)
        if message != self.last_warning:
            self.last_warning = message
            try: self.warning('history unavailable: ' + message)
            except Exception: pass  # diagnostic failure must not escape into a musical operation

    def choice(self, stage, items, weights, chosen, context, roll):
        """items are stable {id,label} records, already in the actual candidate order."""
        try:
            payload = dict(items=items, weights=weights)
            body = encoded(payload)
            key = hashlib.sha256(body.encode('ascii')).hexdigest()
            if key not in self.pool_cache:
                path = self.root / 'pools' / (key + '.json')
                if not path.exists(): atomic(path, payload)
                self.pool_cache.add(key)
            self.emit('choice', dict(stage=stage, pool=key, candidate_count=len(items),
                                    chosen=chosen, context=context), roll)
        except Exception as exc:
            self.warn(exc)

    def tick(self):
        try:
            self.assessment.tick()
        except Exception as exc:
            self.warn(exc)


class Assessment:
    def __init__(self, root, rebuild=False):
        self.root = Path(root)
        self.state_path = self.root / 'health-state.json'
        self.state = dict(schema=SCHEMA, offsets={}, cohorts={}, rolls={}, coverage={}, events=0, seen={}, tails={})
        if not rebuild and self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                if (isinstance(saved,dict) and saved.get('schema') == SCHEMA
                        and isinstance(saved.get('events'),int)
                        and all(isinstance(saved.get(k),dict) for k in ('offsets','cohorts','rolls','coverage','seen','tails'))
                        and all(isinstance(r,dict) and 'selections' in r for r in saved['rolls'].values())):
                    self.state = saved
            except (ValueError, OSError): pass
        self.pools = {}
        self.last_report = 0
        self.dirty = False
        self.file_cursor = 0
        self.flush = None
        self.state.setdefault('seen', {})
        self.state.setdefault('tails', {})

    def count(self, key):
        c = self.state['coverage']; c[key] = c.get(key, 0) + 1

    def consume(self, entry):
        if not isinstance(entry,dict): self.count('malformed_record'); return
        if entry.get('schema') != SCHEMA:
            self.count('unknown_schema'); return
        event_id = entry.get('event_id')
        if not isinstance(event_id, str): self.count('missing_event_id'); return
        if event_id in self.state['seen']: self.count('duplicate_event'); return
        self.state['seen'][event_id] = 1
        self.state['events'] += 1
        self.count('event:' + str(entry.get('event')))
        event, data = entry.get('event'), entry.get('data', {})
        roll = entry.get('roll') or {}
        # Producer-local IDs are globally qualified by session, never by token alone.
        roll_id = entry.get('session', 'import') + '/' + str(roll.get('id', ''))
        if roll.get('id'):
            r = self.state['rolls'].setdefault(roll_id, dict(producer=entry.get('producer'),
                token=roll.get('token'), start=roll.get('started_ms', entry['time_ms']),
                version=entry.get('version'), choices=0, loads=0, outcome='unfinished', selections={}))
            if roll.get('token') is not None: r['token'] = roll['token']
            if event in ('started', 'grant'): r['start'] = entry['time_ms']
            if event == 'terminal':
                r['outcome'] = data.get('outcome', 'unknown'); r['end'] = entry['time_ms']
            if event == 'load' and data.get('operation') in ('hotswap.load.returned','m4l.verified'): r['loads'] += 1
        else: self.count('event_without_roll')
        if event == 'legacy_source': self.count('legacy_source_without_exact_probabilities')
        if event != 'choice': return
        self.count('choices')
        if roll.get('id'): self.state['rolls'][roll_id]['choices'] += 1
        if data.get('pool'):
            key = data['pool']
            if key not in self.pools:
                if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
                    self.count('invalid_pool'); return
                path = self.root / 'pools' / (key + '.json')
                try: self.pools[key] = json.loads(path.read_text())
                except (OSError, ValueError): self.count('missing_pool'); return
            pool = self.pools[key]
        else: pool = data
        items, weights = pool.get('items', []), pool.get('weights', [])
        if not items or len(items) != len(weights) or any(not isinstance(w, (int,float)) or not math.isfinite(w) or w < 0 for w in weights) or sum(weights) <= 0:
            self.count('unknown_probabilities'); return
        names = [item['id'] if isinstance(item, dict) else item for item in items]
        labels = {item['id']:item.get('label',item['id']) for item in items if isinstance(item,dict)}
        if any(isinstance(item,dict) and item.get('identity_known') is False for item in items):
            self.count('unresolved_identity'); return
        probabilities = {}
        total_weight = sum(weights)
        for name, weight in zip(names, weights): probabilities[name] = probabilities.get(name, 0) + weight/total_weight
        chosen = data.get('chosen')
        if chosen not in probabilities or probabilities[chosen] <= 0:
            self.count('ineligible_choice'); return
        context = data.get('context') or {}
        # Raw RNG, slot positions and outcome timing do not define a probability population.
        configuration = {k:v for k,v in context.items() if k not in ('random','total','position')}
        signature = dict(stage=data['stage'], probabilities=probabilities, context=configuration,
                         version=entry.get('version'))
        key = hashlib.sha256(encoded(signature).encode('ascii')).hexdigest()
        cohort = self.state['cohorts'].setdefault(key, dict(configuration=signature, labels=labels,
            n=0, observed={}, first=entry['time_ms'], last=entry['time_ms']))
        cohort['n'] += 1
        cohort['first'] = min(cohort['first'],entry['time_ms'])
        cohort['last'] = max(cohort['last'],entry['time_ms'])
        cohort['observed'][chosen] = cohort['observed'].get(chosen, 0) + 1
        if roll.get('id'):
            selections=self.state['rolls'][roll_id]['selections'].setdefault(key,{})
            selections[chosen]=selections.get(chosen,0)+1
        u = context.get('random')
        if u is not None:
            if not isinstance(u,(int,float)) or not 0 <= u < 1:
                self.count('interval_mismatch'); return
            threshold = u*sum(weights); cumulative=0; expected=None
            for name,weight in zip(names,weights):
                cumulative += weight
                if threshold < cumulative: expected=name; break
            if expected != chosen: self.count('interval_mismatch')

    def report(self, full=False):
        cohorts=[]
        outcomes=collections.defaultdict(lambda:collections.defaultdict(collections.Counter))
        days=collections.defaultdict(collections.Counter)
        for roll in self.state['rolls'].values():
            day=datetime.datetime.fromtimestamp(roll['start']/1000,datetime.timezone.utc).date().isoformat()
            days[day][roll['producer']+':'+roll['outcome']]+=1
            for key,choices in roll['selections'].items():
                for chosen,count in choices.items():outcomes[key][chosen][roll['outcome']]+=count
        selected = list(self.state['cohorts'].items())
        if not full:
            by_stage = collections.defaultdict(list)
            for pair in selected: by_stage[pair[1]['configuration']['stage']].append(pair)
            selected = [pair for stage in by_stage.values() for pair in sorted(stage,key=lambda p:-p[1]['n'])[:12]]
        for key,c in selected:
            rows=[]
            for name,p in c['configuration']['probabilities'].items():
                expected=c['n']*p; actual=c['observed'].get(name,0); variance=c['n']*p*(1-p)
                z=(actual-expected)/math.sqrt(variance) if variance>0 else 0
                assessable=c['n']>=30 and expected>=5
                flag=assessable and abs(z)>=3
                rows.append(dict(id=name,label=c['labels'].get(name,name),observed=actual,expected=expected,
                                 standardized_difference=z,review_deviation=flag,
                                 status='review_deviation' if flag else 'no_large_deviation' if assessable else 'insufficient_data',
                                 roll_outcomes=dict(outcomes[key][name])))
            review_count = sum(r['review_deviation'] for r in rows)
            assessable_count = sum(r['status']!='insufficient_data' for r in rows)
            if not full: rows = sorted(rows,key=lambda r:(-r['review_deviation'],-abs(r['standardized_difference']),-r['observed']))[:100]
            cohorts.append(dict(id=key,stage=c['configuration']['stage'],context=c['configuration']['context'],
                                version=c['configuration']['version'],draws=c['n'],first=c['first'],last=c['last'],
                                status='review_deviation' if review_count else
                                'insufficient_data' if not assessable_count else 'no_large_deviation',choices=rows,
                                total_choices=len(c['configuration']['probabilities']),review_count=review_count))
        rolls=list(self.state['rolls'].values())
        engine=[r for r in rolls if r['producer']=='engine' and r['token'] is not None]
        companion=[r for r in rolls if r['producer']=='companion' and r['token'] is not None]
        joins=collections.Counter();joined_outcomes=collections.Counter()
        engine_by_token=collections.defaultdict(list); companion_by_token=collections.defaultdict(list)
        for r in engine: engine_by_token[r['token']].append(r)
        for r in companion: companion_by_token[r['token']].append(r)
        for r in engine:
            matches=[c for c in companion_by_token[r['token']] if abs(c['start']-r['start'])<=30000]
            reverse=[e for e in engine_by_token[r['token']] if matches and abs(e['start']-matches[0]['start'])<=30000]
            joins['unique' if len(matches)==len(reverse)==1 else 'missing' if not matches else 'ambiguous']+=1
            if len(matches)==len(reverse)==1:
                joined_outcomes['engine:'+r['outcome']+' / companion:'+matches[0]['outcome']]+=1
        return dict(schema=SCHEMA,updated_ms=int(time.time()*1000),events=self.state['events'],
                    coverage=self.state['coverage'],incomplete_tails=self.state['tails'],
                    cohort_count=len(self.state['cohorts']),report_complete=full,
                    report_limits=None if full else dict(cohorts_per_stage=12,choices_per_cohort=100),
                    roll_outcomes=dict(collections.Counter(r['producer']+':'+r['outcome'] for r in rolls)),
                    daily_roll_outcomes_utc=dict(sorted(days.items())),
                    roll_joins=dict(joins),joined_roll_outcomes=dict(joined_outcomes),cohorts=sorted(cohorts,key=lambda c:-c['draws']),
                    interpretation='Counts describe selected candidates, not guaranteed successful loads. Cohorts retain exact pools/weights/context. Deviation flags are descriptive screening (n>=30, expected>=5, |z|>=3), not sequential significance tests or a health certificate. Missing/ambiguous joins and unfinished rolls remain explicit.')

    def _publish(self):
        # Pause ingestion while serializing this consistent checkpoint in bounded chunks.
        for path, value in ((self.state_path,self.state),(self.root/'health.json',self.report())):
            temporary=path.with_suffix('.json.tmp-'+str(os.getpid()))
            with temporary.open('w',encoding='ascii') as stream:
                buffer=[]; size=0
                for chunk in json.JSONEncoder(sort_keys=True,ensure_ascii=True,separators=(',',':')).iterencode(value):
                    buffer.append(chunk);size+=len(chunk)
                    if size>=16384:
                        stream.write(''.join(buffer));buffer=[];size=0;yield
                stream.write(''.join(buffer)+'\n')
            os.replace(str(temporary),str(path))
            yield
        self.last_report=time.monotonic();self.dirty=False

    def tick(self, budget_ms=8, max_events=100):
        started=time.monotonic(); processed=0
        if self.flush is not None:
            try:
                while (time.monotonic()-started)*1000<budget_ms: next(self.flush)
            except StopIteration: self.flush=None
            return 1  # publishing work remains distinguishable from an idle reader
        files=sorted(self.root.glob('*.jsonl'))
        if files:
            self.file_cursor %= len(files)
            files=files[self.file_cursor:]+files[:self.file_cursor]
        for path in files:
            name=path.name; offset=self.state['offsets'].get(name,0)
            if path.stat().st_size<offset:
                self.state['tails'][name]=dict(truncated=True,previous_offset=offset,size=path.stat().st_size)
                self.dirty=True
                continue  # never count an overwritten prefix twice
            with path.open('rb') as stream:
                stream.seek(offset)
                while processed<max_events and (time.monotonic()-started)*1000<budget_ms:
                    line=stream.readline(2*1024*1024)
                    if not line: self.state['tails'].pop(name,None); break
                    if not line.endswith(b'\n'):
                        tail=dict(offset=stream.tell()-len(line),bytes=len(line))
                        if self.state['tails'].get(name)!=tail: self.dirty=True
                        self.state['tails'][name]=tail;break
                    self.state['tails'].pop(name,None)
                    try: self.consume(json.loads(line))
                    except (ValueError,KeyError,TypeError,OverflowError,AttributeError): self.count('malformed_record')
                    self.state['offsets'][name]=stream.tell();processed+=1;self.dirty=True
            self.file_cursor += 1
            if processed>=max_events or (time.monotonic()-started)*1000>=budget_ms:break
        if self.dirty and time.monotonic()-self.last_report>2:
            self.flush=self._publish()
        return processed


if __name__ == '__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('history',type=Path)
    parser.add_argument('--rebuild',action='store_true')
    args=parser.parse_args()
    assessment=Assessment(args.history,args.rebuild)
    while assessment.tick(budget_ms=1000,max_events=10000) or assessment.flush:pass
    atomic(assessment.state_path,assessment.state)
    atomic(args.history/'health.json',assessment.report(full=True))
    print(args.history/'health.json')
