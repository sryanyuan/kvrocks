import collections
import csv
import json
import re
import statistics
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
LOGS = sorted((ROOT / 'temp/db').glob('LOG*'), key=lambda p: (p.name == 'LOG', p.name))
files = {}
jobs = {}
daily = {}
states = []
stalls = []
snapshots = []
samples = []
errors = []
options = {}
totals = collections.Counter()
job_cf = {}
current_stats = None
stats_cf = None
current_time = ''
last_state = None
first_state = None
peak_pending = None
first_soft_state = None
cf_pattern = re.compile('\\[([^\\]]+)\\] \\[JOB (\\d+)\\] (?:Compacting|Flushing)')
move_pattern = re.compile('\\[([^\\]]+)\\] Moving #(\\d+) to level-(\\d+) (\\d+) bytes')
state_pattern = re.compile('\\[metadata\\].*base level (\\d+) level multiplier ([\\d.]+) max bytes base (\\d+) files\\[([^]]+)\\] max score ([\\d.]+), estimated pending compaction bytes (\\d+)')
stall_pattern = re.compile('\\[([^]]+)\\] (Stalling|Stopping) writes because of (.*)')
level_pattern = re.compile('^\\s*L(\\d+)\\s+(\\d+)/(\\d+)\\s+([\\d.]+)\\s+(KB|MB|GB|TB)\\s+([\\d.]+)')
job_writer_file = (OUT / 'jobs.csv').open('w')
job_writer = csv.writer(job_writer_file)
job_writer.writerow(['time', 'job', 'cf', 'reason', 'output_level', 'input_bytes', 'output_bytes', 'input_records', 'output_records', 'output_files', 'small_outputs', 'duration_us', 'cpu_us', 'subcompactions', 'compression', 'source', 'line'])

def day_record(day):
    if day not in daily:
        daily[day] = {'counts': collections.Counter(), 'reasons': collections.Counter(), 'last_state': None, 'max_pending': 0, 'min_rate': None, 'max_rate': None}
    return daily[day]

def inventory():
    result = {}
    for level in range(7):
        selected = [v for v in files.values() if v['cf'] == 'metadata' and v['level'] == level]
        sizes = sorted((v['size'] for v in selected))
        result[str(level)] = {'known_files': len(selected), 'known_bytes': sum(sizes), 'lt1MiB': sum((x < 2 ** 20 for x in sizes)), 'lt16MiB': sum((x < 16 * 2 ** 20 for x in sizes)), 'lt1MiB_bytes': sum((x for x in sizes if x < 2 ** 20)), 'median': statistics.median(sizes) if sizes else 0, 'birth_levels': dict(collections.Counter((str(v['birth_level']) for v in selected))), 'small_birth_levels': dict(collections.Counter((str(v['birth_level']) for v in selected if v['size'] < 2 ** 20))), 'min': sizes[0] if sizes else 0, 'max': sizes[-1] if sizes else 0}
    return result
for path in LOGS:
    print('Scanning', path.name, flush=True)
    with path.open(errors='replace') as handle:
        for (line_no, line) in enumerate(handle, 1):
            if len(line) > 26 and line[4:5] == '/':
                current_time = line[:26]
            day = current_time[:10]
            d = day_record(day)
            ref = [path.name, line_no]
            if 'Options.' in line:
                match = re.search('Options\\.([\\w.]+)\\s*:\\s*(.*)', line)
                if match:
                    options.setdefault(match[1], {})[match[2].strip()] = ref
            if 'base level ' in line and '[metadata]' in line:
                match = state_pattern.search(line)
                if match:
                    state = {'time': current_time, 'base_level': int(match[1]), 'multiplier': float(match[2]), 'base_bytes': int(match[3]), 'files': list(map(int, match[4].split())), 'score': float(match[5]), 'pending': int(match[6]), 'source': ref}
                    first_state = first_state or state
                    last_state = state
                    d['last_state'] = state
                    d['max_pending'] = max(d['max_pending'], state['pending'])
                    if peak_pending is None or state['pending'] > peak_pending['pending']:
                        peak_pending = state
                    if first_soft_state is None and state['pending'] >= 64 * 2 ** 30:
                        first_soft_state = state
                    hour = current_time[:13]
                    if not states or states[-1]['time'][:13] != hour:
                        states.append(state)
                    else:
                        states[-1] = state
            if 'Stalling writes' in line or 'Stopping writes' in line:
                match = stall_pattern.search(line)
                if match:
                    entry = {'time': current_time, 'cf': match[1], 'action': match[2], 'cause': match[3].strip(), 'source': ref}
                    stalls.append(entry)
                    d['counts']['stall_' + match[2]] += 1
                    rate = re.search(' rate (\\d+)', line)
                    if rate:
                        n = int(rate[1])
                        d['min_rate'] = min(d['min_rate'] or n, n)
                        d['max_rate'] = max(d['max_rate'] or n, n)
            if 'DUMPING STATS' in line:
                current_stats = {'time': current_time, 'source': ref, 'cfs': {}, 'db': []}
                snapshots.append(current_stats)
                stats_cf = None
            if current_stats is not None:
                if line.startswith('** Compaction Stats ['):
                    stats_cf = line.split('[')[1].split(']')[0]
                    current_stats['cfs'].setdefault(stats_cf, {'levels': {}, 'details': []})
                elif stats_cf:
                    match = level_pattern.match(line)
                    if match:
                        current_stats['cfs'][stats_cf]['levels'][match[1]] = {'files': int(match[2]), 'compacting': int(match[3]), 'size': float(match[4]), 'unit': match[5], 'score': float(match[6]), 'source': ref, 'raw': line.strip()}
                    elif line.startswith(('Estimated pending compaction bytes:', 'Write Stall (count):', 'Cumulative compaction:', 'Interval compaction:', 'Block cache entry stats')):
                        current_stats['cfs'][stats_cf]['details'].append({'text': line.strip(), 'source': ref})
                if line.startswith(('Cumulative stall:', 'Interval stall:', 'Interval writes:', 'Uptime(secs):')) and stats_cf is None:
                    current_stats['db'].append({'text': line.strip(), 'source': ref})
            match = cf_pattern.search(line) if '[JOB ' in line else None
            if match:
                job_cf[int(match[2])] = match[1]
            if 'Moving #' in line:
                match = move_pattern.search(line)
                if match:
                    (cf, fid, level, size) = (match[1], int(match[2]), int(match[3]), int(match[4]))
                    v = files.setdefault(fid, {'cf': cf, 'size': size, 'birth_level': None, 'level': None, 'created': None, 'entries': None, 'job': None, 'source': None})
                    previous = v['level']
                    v['level'] = level
                    v['move'] = {'time': current_time, 'source': ref}
                    if cf == 'metadata' and level == 6:
                        d['counts']['moves_L6'] += 1
                        d['counts']['moves_L6_bytes'] += size
                        if size < 2 ** 20:
                            d['counts']['small_moves_L6'] += 1
                            d['counts']['small_moves_L6_bytes'] += size
                        if previous is None:
                            totals['moves_with_unknown_origin'] += 1
            if 'EVENT_LOG_v1 {' not in line:
                if ('[ERROR]' in line or 'No space left' in line or 'IO error' in line) and len(errors) < 100:
                    errors.append({'source': ref, 'text': line.strip()})
                continue
            try:
                event = json.loads(line.split('EVENT_LOG_v1 ', 1)[1])
            except json.JSONDecodeError:
                totals['json_errors'] += 1
                continue
            kind = event['event']
            totals[kind] += 1
            jid = event.get('job')
            if kind == 'table_file_creation':
                props = event.get('table_properties', {})
                fid = event['file_number']
                files[fid] = {'cf': event['cf_name'], 'size': event['file_size'], 'birth_level': None, 'level': None, 'created': current_time, 'entries': props.get('num_entries'), 'job': jid, 'source': ref, 'raw_bytes': props.get('raw_key_size', 0) + props.get('raw_value_size', 0), 'data_bytes': props.get('data_size', 0), 'compression': props.get('compression'), 'file_creation_time': props.get('file_creation_time')}
                job_cf[jid] = event['cf_name']
                jobs.setdefault(jid, {'outputs': []})['outputs'].append(fid)
            elif kind == 'compaction_started':
                jobs.setdefault(jid, {'outputs': []})['start'] = event
                jobs[jid]['time'] = current_time
                jobs[jid]['source'] = ref
            elif kind in ('compaction_finished', 'flush_finished'):
                job = jobs.pop(jid, {'outputs': []})
                start = job.get('start', {})
                cf = job_cf.pop(jid, None)
                level = event.get('output_level', 0)
                output_ids = job['outputs']
                small = 0
                for fid in output_ids:
                    if fid not in files:
                        continue
                    v = files[fid]
                    v['level'] = level
                    v['birth_level'] = level
                    v['job_no_record_drops'] = event.get('num_input_records', -1) == event.get('num_output_records', -2)
                    small += v['size'] < 2 ** 20
                for (key, fids) in start.items():
                    if key.startswith('files_L'):
                        for fid in fids:
                            files.pop(fid, None)
                if cf == 'metadata':
                    d['counts'][f'created_L{level}'] += len(output_ids)
                    d['counts'][f'small_created_L{level}'] += small
                    d['counts'][f'created_L{level}_bytes'] += event.get('total_output_size', sum((files[x]['size'] for x in output_ids if x in files)))
                    if kind == 'compaction_finished':
                        reason = start.get('compaction_reason', 'unknown')
                        d['reasons'][reason] += 1
                        d['counts']['compaction_input_bytes'] += start.get('input_data_size', 0)
                        d['counts']['compaction_output_bytes'] += event.get('total_output_size', 0)
                        d['counts']['compaction_us'] += event['compaction_time_micros']
                        d['counts']['compaction_cpu_us'] += event['compaction_time_cpu_micros']
                        d['counts'][f'jobs_L{level}'] += 1
                        d['counts'][f'jobs_L{level}_us'] += event['compaction_time_micros']
                        job_writer.writerow([job.get('time', current_time), jid, cf, reason, level, start.get('input_data_size'), event.get('total_output_size'), event.get('num_input_records'), event.get('num_output_records'), event.get('num_output_files'), small, event['compaction_time_micros'], event['compaction_time_cpu_micros'], event.get('num_subcompactions'), event.get('output_compression'), path.name, line_no])
                        if level == 5 and small > 0 and (len(samples) < 10):
                            samples.append({'job': jid, 'start': start, 'end': event, 'source': ref, 'small_outputs': [dict(files[x], file=x) for x in output_ids if x in files and files[x]['size'] < 2 ** 20][:5]})
            elif kind == 'table_file_deletion':
                files.pop(event['file_number'], None)
    daily[current_time[:10]]['known_inventory_at_file_end'] = inventory()
    print('Known live files:', len(files), 'last state:', last_state['files'] if last_state else None, flush=True)
job_writer_file.close()
summary = {'logs': [{'name': p.name, 'bytes': p.stat().st_size} for p in LOGS], 'events': dict(totals), 'first_state': first_state, 'last_state': last_state, 'peak_pending': peak_pending, 'first_soft_state': first_soft_state, 'daily': daily, 'options': options, 'known_final_inventory': inventory(), 'samples': samples, 'errors': errors}
(OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
(OUT / 'stats-snapshots.json').write_text(json.dumps(snapshots, ensure_ascii=False, indent=2) + '\n')
(OUT / 'hourly-states.json').write_text(json.dumps(states, ensure_ascii=False, indent=2) + '\n')
(OUT / 'stall-events.json').write_text(json.dumps(stalls, ensure_ascii=False, indent=2) + '\n')
with (OUT / 'known-live-files.jsonl').open('w') as handle:
    for (fid, v) in files.items():
        handle.write(json.dumps(dict(v, file=fid), ensure_ascii=False) + '\n')
print('Complete:', len(snapshots), 'stats snapshots;', len(stalls), 'stall events', flush=True)
