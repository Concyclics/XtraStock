"""Keep daily work bounded without losing older dirty intervals."""
import datetime as dt


def choose_window(candidate, as_of, mode, daily_days=3, repair_since=None):
    if mode not in ('daily', 'repair') or daily_days < 0:
        raise ValueError('invalid replay mode or daily window')
    if repair_since and mode != 'repair':
        raise ValueError('--repair-since requires --mode repair')
    start = repair_since or candidate
    if start > as_of:
        raise ValueError('replay starts after data date')
    return start if mode == 'repair' else max(candidate, as_of-dt.timedelta(days=daily_days))


def retain_older(conn, pipeline, candidate, actual_since, cutoff):
    if candidate < actual_since:
        conn.execute("""INSERT INTO quant.repair_queue(pipeline,since_date,until_date,source_cutoff)
          VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
          (pipeline,candidate,actual_since-dt.timedelta(days=1),cutoff))


def finish_covered(conn, pipeline, since, until, cutoff):
    conn.execute("""UPDATE quant.repair_queue SET status='complete',completed_at=clock_timestamp()
      WHERE pipeline=%s AND status='pending' AND since_date>=%s AND until_date<=%s AND source_cutoff<=%s""",
      (pipeline,since,until,cutoff))
