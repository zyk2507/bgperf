# Copyright (C) 2026
#
# Licensed under the Apache License, Version 2.0 (the "License");

import datetime
import json
import os


METRICS_FILE = 'metrics.jsonl'
RUN_FILE = 'run.json'
REPORT_FILE = 'report.md'


def utc_now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


def mem_human(value):
    value = int(value or 0)
    if value > 1000 * 1000 * 1000:
        return '{0:.2f}GB'.format(float(value) / (1000 * 1000 * 1000))
    if value > 1000 * 1000:
        return '{0:.2f}MB'.format(float(value) / (1000 * 1000))
    if value > 1000:
        return '{0:.2f}KB'.format(float(value) / 1000)
    return '{0:.2f}B'.format(float(value))


def expected_routes(conf):
    checkpoints = conf.get('monitor', {}).get('check-points') or []
    if checkpoints:
        return int(checkpoints[-1])

    total = 0
    for tester in conf.get('testers', []):
        for neighbor in tester.get('neighbors', {}).values():
            paths = neighbor.get('paths') or []
            total += len(paths)
    return total


def container_label(name):
    prefixes = [
        'bgperf_',
        'bgperf-',
    ]
    for prefix in prefixes:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def format_resource_status(latest, order):
    parts = []
    seen = set()
    for name in order:
        if name in latest:
            item = latest[name]
            parts.append('{0}={1:.2f}%/{2}'.format(
                container_label(name), item.get('cpu', 0.0), mem_human(item.get('mem', 0))
            ))
            seen.add(name)
    for name in sorted(k for k in latest.keys() if k not in seen):
        item = latest[name]
        parts.append('{0}={1:.2f}%/{2}'.format(
            container_label(name), item.get('cpu', 0.0), mem_human(item.get('mem', 0))
        ))
    return ', '.join(parts) if parts else 'collecting'


class BenchReporter(object):
    def __init__(self, config_dir, enabled=True):
        self.config_dir = config_dir
        self.enabled = enabled
        self.metrics_path = os.path.join(config_dir, METRICS_FILE)
        self.run_path = os.path.join(config_dir, RUN_FILE)
        if self.enabled:
            if not os.path.exists(config_dir):
                os.makedirs(config_dir)
            open(self.metrics_path, 'w').close()

    def start(self, metadata):
        if not self.enabled:
            return
        data = dict(metadata)
        data['started_at'] = data.get('started_at') or utc_now()
        self.write_run(data)

    def finish(self, metadata, report_path=None):
        if not self.enabled:
            return None
        data = self.read_run()
        data.update(metadata)
        data['ended_at'] = data.get('ended_at') or utc_now()
        self.write_run(data)
        return write_report_from_run(self.config_dir, report_path)

    def write_run(self, metadata):
        with open(self.run_path, 'w') as f:
            json.dump(metadata, f, indent=2, sort_keys=True)

    def read_run(self):
        if not os.path.isfile(self.run_path):
            return {}
        with open(self.run_path) as f:
            return json.load(f)

    def record_resource(self, elapsed, container, cpu, mem):
        self.record({
            'type': 'resource',
            'time': utc_now(),
            'elapsed': int(elapsed),
            'container': container,
            'cpu': float(cpu or 0),
            'mem': int(mem or 0),
        })

    def record_routes(self, elapsed, received):
        self.record({
            'type': 'routes',
            'time': utc_now(),
            'elapsed': int(elapsed),
            'received': int(received or 0),
        })

    def record(self, event):
        if not self.enabled:
            return
        with open(self.metrics_path, 'a') as f:
            f.write(json.dumps(event, sort_keys=True) + '\n')


class MetricSummary(object):
    def __init__(self):
        self.containers = {}
        self.routes = []
        self.elapsed_max = 0

    def add(self, event):
        self.elapsed_max = max(self.elapsed_max, int(event.get('elapsed') or 0))
        if event.get('type') == 'resource':
            self.add_resource(event)
        elif event.get('type') == 'routes':
            self.routes.append(event)

    def add_resource(self, event):
        name = event.get('container')
        if not name:
            return
        item = self.containers.setdefault(name, {
            'count': 0,
            'cpu_sum': 0.0,
            'cpu_max': 0.0,
            'mem_sum': 0,
            'mem_max': 0,
            'last_cpu': 0.0,
            'last_mem': 0,
        })
        cpu = float(event.get('cpu') or 0)
        mem = int(event.get('mem') or 0)
        item['count'] += 1
        item['cpu_sum'] += cpu
        item['cpu_max'] = max(item['cpu_max'], cpu)
        item['mem_sum'] += mem
        item['mem_max'] = max(item['mem_max'], mem)
        item['last_cpu'] = cpu
        item['last_mem'] = mem

    def final_received(self):
        if not self.routes:
            return 0
        return int(self.routes[-1].get('received') or 0)

    def duration(self):
        return self.elapsed_max


def read_metrics(path):
    summary = MetricSummary()
    if not os.path.isfile(path):
        raise ValueError('metrics file not found: {0}'.format(path))
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                summary.add(json.loads(line))
    return summary


def read_run_metadata(config_dir):
    path = os.path.join(config_dir, RUN_FILE)
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def write_report_from_run(config_dir, output_path=None):
    metrics_path = os.path.join(config_dir, METRICS_FILE)
    summary = read_metrics(metrics_path)
    metadata = read_run_metadata(config_dir)
    output_path = output_path or os.path.join(config_dir, REPORT_FILE)
    output_path = os.path.abspath(os.path.expanduser(output_path))
    output_dir = os.path.dirname(output_path)
    if output_dir and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    with open(output_path, 'w') as f:
        write_report(f, config_dir, metadata, summary)
    return output_path


def write_report(f, config_dir, metadata, summary):
    f.write('# bgperf Benchmark Report\n\n')
    f.write('* Generated: {0}\n'.format(utc_now()))
    f.write('* Run directory: `{0}`\n'.format(os.path.abspath(config_dir)))
    f.write('* Runtime: `{0}`\n'.format(metadata.get('runtime', 'unknown')))
    f.write('* Target: `{0}`\n'.format(metadata.get('target', 'unknown')))
    f.write('* Local prefix: `{0}`\n'.format(metadata.get('local_prefix', 'unknown')))
    f.write('* Neighbor count: `{0}`\n'.format(metadata.get('neighbor_count', 'unknown')))
    f.write('* Expected routes: `{0}`\n'.format(metadata.get('expected_routes', 'unknown')))
    f.write('* Final received routes: `{0}`\n'.format(summary.final_received()))
    f.write('* Duration: `{0}s`\n\n'.format(summary.duration()))

    f.write('## Container Resource Usage\n\n')
    f.write('| Container | Samples | Avg CPU | Max CPU | Avg Mem | Max Mem | Last Mem |\n')
    f.write('| --- | ---: | ---: | ---: | ---: | ---: | ---: |\n')
    for name in sorted(summary.containers.keys()):
        item = summary.containers[name]
        count = item['count'] or 1
        f.write('| `{0}` | {1} | {2:.2f}% | {3:.2f}% | {4} | {5} | {6} |\n'.format(
            name,
            item['count'],
            item['cpu_sum'] / count,
            item['cpu_max'],
            mem_human(item['mem_sum'] / count),
            mem_human(item['mem_max']),
            mem_human(item['last_mem']),
        ))

    f.write('\n## Route Progress\n\n')
    f.write('| Elapsed | Received |\n')
    f.write('| ---: | ---: |\n')
    for event in compact_routes(summary.routes):
        f.write('| {0}s | {1} |\n'.format(
            int(event.get('elapsed') or 0),
            int(event.get('received') or 0),
        ))

    f.write('\nRaw samples are stored in `{0}`.\n'.format(METRICS_FILE))


def compact_routes(routes, limit=20):
    if len(routes) <= limit:
        return routes
    head = routes[:10]
    tail = routes[-10:]
    return head + tail
