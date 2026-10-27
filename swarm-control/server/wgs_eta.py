"""Operational ETA from monotonic, timestamped work counters (no external I/O)."""
import math


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def estimate_eta(observations, total, *, now, blocked=False, window=900,
                 min_span=60, stale_after=120, stall_after=180):
    """Estimate remaining work from [{timestamp, completed, generation?}].

    Units may be bytes, reads or tasks, but must agree with total. Call once
    per independently measured stage. Missing work counters must be omitted,
    never replaced with zero. Generation changes and counter regressions reset
    the rate window. Bounds are observed-rate sensitivity, not confidence limits.
    No ETA is returned for blocked, stale, stalled or insufficient observations.
    """
    if not _finite(total) or not _finite(now):
        raise ValueError('total and now must be finite nonnegative numbers')
    if any(not _finite(v) or v <= 0 for v in (window, min_span, stale_after, stall_after)):
        raise ValueError('timing parameters must be finite and positive')
    points=[]
    for row in observations:
        t=row.get('timestamp');done=row.get('completed')
        if not _finite(t) or not _finite(done) or t > now or done > total:
            continue
        # Reject out-of-order samples rather than inventing a chronology.
        if points and t < points[-1]['timestamp']:
            continue
        if points and (row.get('generation') != points[-1].get('generation') or done < points[-1]['completed']):
            points=[]
        point={'timestamp':t,'completed':done,'generation':row.get('generation')}
        if points and t == points[-1]['timestamp']:
            points[-1]=point
        else:
            points.append(point)
    result={'status':'insufficient_data','seconds_remaining':None,
            'optimistic_seconds':None,'pessimistic_seconds':None,
            'rate_per_second':None,'remaining':None,'observed_seconds':0,
            'bounds_kind':'observed_rate_sensitivity','includes_wait_time':False}
    if not points:
        return {**result,'status':'blocked' if blocked else 'insufficient_data'}
    latest=points[-1];result['remaining']=total-latest['completed']
    if now-latest['timestamp'] > stale_after:
        return {**result,'status':'stale'}
    if latest['completed'] == total:
        return {**result,'status':'complete','seconds_remaining':0,
                'optimistic_seconds':0,'pessimistic_seconds':0}
    if blocked:
        return {**result,'status':'blocked'}
    # Keep one predecessor so chunked counters include idle intervals.
    cutoff=now-window
    start=next((i for i,p in enumerate(points) if p['timestamp'] >= cutoff),len(points)-1)
    points=points[max(0,start-1):]
    span=latest['timestamp']-points[0]['timestamp'];result['observed_seconds']=span
    changes=[p['timestamp'] for a,p in zip(points,points[1:]) if p['completed']>a['completed']]
    last_change=changes[-1] if changes else points[0]['timestamp']
    if now-last_change >= stall_after:
        return {**result,'status':'stalled'}
    if span < min_span or not changes:
        return result
    rate=(latest['completed']-points[0]['completed'])/span
    # Compute slopes over windows >= min_span, including periods without work;
    # short chunk-completion bursts cannot masquerade as sustainable throughput.
    slopes=[]
    anchor=points[0]
    for point in points[1:]:
        dt=point['timestamp']-anchor['timestamp']
        if dt >= min_span:
            slopes.append((point['completed']-anchor['completed'])/dt)
            anchor=point
    positive=[v for v in slopes if v>0]+[rate]
    low=min(slopes+[rate]);high=max(positive)
    return {**result,'status':'estimating','rate_per_second':rate,
            'seconds_remaining':result['remaining']/rate,
            'optimistic_seconds':result['remaining']/high,
            'pessimistic_seconds':result['remaining']/low if low>0 else None}
