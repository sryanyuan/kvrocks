import collections
import datetime
import json
import re
import statistics
import subprocess
from pathlib import Path
OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
paths = sorted((ROOT / 'temp/db').glob('LOG*'), key=lambda p: (p.name == 'LOG', p.name))
process = subprocess.Popen(['rg', '--no-heading', '--with-filename', '--line-number', 'Moving #|"event": "trivial_move"', *map(str, paths)], stdout=subprocess.PIPE, text=True)
pending = {}
daily = collections.defaultdict(list)
examples = {}
invalid = 0

def timestamp(line):
    match = re.search('Original Log Time ([\\d/]+-[\\d:.]+)', line)
    value = match[1] if match else line[:26]
    return datetime.datetime.strptime(value, '%Y/%m/%d-%H:%M:%S.%f')
for raw in process.stdout:
    (filename, line_number, line) = raw.split(':', 2)
    thread = line.split()[1]
    if 'Moving #' in line:
        match = re.search('\\[metadata\\] Moving #(\\d+) to level-(\\d+) (\\d+) bytes', line)
        if match:
            pending.setdefault(thread, []).append({'time': timestamp(line), 'file': int(match[1]), 'level': int(match[2]), 'size': int(match[3]), 'source': [Path(filename).name, int(line_number)]})
    elif 'EVENT_LOG_v1 ' in line:
        event = json.loads(line.split('EVENT_LOG_v1 ', 1)[1])
        moves = pending.pop(thread, [])
        if event['files'] == 1 and len(moves) == 1 and (moves[0]['level'] == 6):
            move = moves[0]
            seconds = (timestamp(line) - move['time']).total_seconds()
            if seconds < 0:
                invalid += 1
                continue
            day = move['time'].strftime('%Y/%m/%d')
            daily[day].append(seconds)
            example = dict(move, time=str(move['time']), seconds=seconds, job=event['job'], end_source=[Path(filename).name, int(line_number)])
            if day not in examples or seconds > examples[day]['seconds']:
                examples[day] = example
process.wait()
assert process.returncode in (0, 1)
result = {'method': 'Elapsed log time from a single-file metadata L6 Moving record before LogAndApply to its same-thread trivial_move event after InstallSuperVersionAndScheduleWork; includes waits and scheduling, not CPU time.', 'invalid': invalid, 'daily': {}}
for (day, values) in sorted(daily.items()):
    values.sort()
    result['daily'][day] = {'count': len(values), 'median_ms': statistics.median(values) * 1000, 'p90_ms': values[int(0.9 * (len(values) - 1))] * 1000, 'p99_ms': values[int(0.99 * (len(values) - 1))] * 1000, 'mean_ms': statistics.mean(values) * 1000, 'max_ms': max(values) * 1000, 'max_example': examples[day]}
(OUT / 'move-latency.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
