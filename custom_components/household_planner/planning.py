"""Seven calendar days, three days of advancement; no source mutations."""
import copy
import re
from datetime import timedelta
from itertools import combinations
NO_VACUUM_WEEKDAY = 6


def canonical_type(task, metadata=None):
    title = task.get('title', '').casefold()
    if re.search(r'b[oö]den?.*wisch|wisch.*b[oö]den?|balkon.*wisch|\bmopp?en\b|\bmop\b', title):
        return 'mop'
    if (metadata or {}).get('contains_vacuuming') is True or re.search(r'staubsaug|\bsaugen\b|\bvacuum\b', title):
        return 'vacuum'
    for kind, pattern in [('trash', r'müll|abfall'), ('laundry', r'wäsche|waschmaschine'),
                          ('plants', r'pflanz|giess|gieß'), ('cat', r'katze'),
                          ('bed', r'bett'), ('bath', r'waschbecken|badewanne|dusche'),
                          ('kitchen', r'geschirr|spülmaschine')]:
        if re.search(pattern, title):
            return kind
    return 'unknown'


def room(entry):
    return ' '.join(entry.get('area', '').casefold().split())


def compatible(entry, entries, day):
    if entry['contains_vacuuming'] and day.weekday() == NO_VACUUM_WEEKDAY:
        return False
    for other in entries:
        if entry['task_id'] == other['task_id'] or entry['occurrence_id'] == other['occurrence_id']:
            return False
        if room(entry) and room(entry) == room(other) and {entry['canonical_task_type'], other.get('canonical_task_type')} == {'vacuum', 'mop'}:
            return False
    return True


def pack(candidates, capacity, preserved, day):
    """Exact grouped knapsack; stable input order breaks equal-point ties."""
    mopped = {room(e) for e in candidates + preserved if e.get('canonical_task_type') == 'mop' and room(e)}
    groups = {}
    for e in candidates:
        if e['canonical_task_type'] == 'vacuum' and room(e) in mopped:
            continue
        if not compatible(e, preserved, day):
            continue
        # Two floor chores of the same type are still separate canonical tasks.
        groups.setdefault(e['task_id'], []).append(e)
    capacity = max(0, min(capacity, sum(max(e['weight'] for e in group) for group in groups.values())))
    states = {0: []}
    for group in groups.values():
        updated = dict(states)
        for points, selected in states.items():
            for e in group:
                new = points + e['weight']
                if new <= capacity and new not in updated:
                    updated[new] = selected + [e]
        states = updated
    return states[max(states)]


def schedule(start, budgets, candidates, preserved=(), *, smoothing=True):
    days = [start + timedelta(days=i) for i in range(7)]
    result = [copy.deepcopy(list(preserved))] + [[] for _ in range(6)]
    waiting = []
    # Place regular/overdue work first, optimizing points without a count limit.
    for index, day in enumerate(days):
        pool = [e for e in candidates if max(start, e['_due_day']) == day]
        selected = pack(pool, budgets[index] - sum(e['weight'] for e in result[index]), result[index], day)
        result[index].extend(selected)
        ids = {e['occurrence_id'] for e in selected}
        mopped = {room(e) for e in pool + result[index] if e.get('canonical_task_type') == 'mop' and room(e)}
        waiting.extend(e for e in pool if e['occurrence_id'] not in ids and not
                       (e['canonical_task_type'] == 'vacuum' and room(e) in mopped))

    attempts = 0
    def _place(entry, target, trail):
        nonlocal attempts
        attempts += 1
        if attempts > 20000 or entry['occurrence_id'] in trail:
            return False
        if target < 0 or days[target] < entry['_due_day'] - timedelta(days=3):
            return False
        if entry.get('_projected') and target == 0:
            return False
        if entry.get('_fixed') or not compatible(entry, result[target], days[target]):
            return False
        used = sum(e['weight'] for e in result[target])
        needed = used + entry['weight'] - budgets[target]
        if needed <= 0:
            result[target].append(entry)
            return True
        movable = [e for e in result[target] if e not in preserved and not e.get('_fixed')]
        # Weights are 1..3, so at most three blockers need to leave a day.
        for count in range(1, min(3, len(movable)) + 1):
            for blockers in combinations(movable, count):
                attempts += 1
                if attempts > 20000:
                    return False
                if sum(e['weight'] for e in blockers) < needed:
                    continue
                old = [list(es) for es in result]
                ok = True
                for blocker in blockers:
                    result[target].remove(blocker)
                    if not any(place(blocker, earlier, trail | {entry['occurrence_id']})
                               for earlier in range(target-1, -1, -1)):
                        ok = False
                        break
                if ok:
                    result[target].append(entry)
                    return True
                result[:] = old
        return False

    def place(entry, target, trail):
        old = [list(es) for es in result]
        if 0 <= target < 7 and entry['canonical_task_type'] == 'mop' and room(entry):
            # Wiping wins over uncompleted vacuum work, including a cascade target.
            result[target] = [e for e in result[target] if not
                              (e.get('canonical_task_type') == 'vacuum' and room(e) == room(entry) and e not in preserved)]
        if _place(entry, target, trail):
            return True
        result[:] = old
        return False

    unplaced = []
    for entry in waiting:
        index = (max(start, entry['_due_day']) - start).days
        # Only overloaded future work advances. Spare capacity alone is no reason.
        if not any(place(entry, earlier, set()) for earlier in range(index-1, -1, -1)):
            unplaced.append(entry)
    # Overdue/today work cannot advance, but may use later capacity as backlog.
    for entry in unplaced:
        if entry.get('_fixed'):
            continue
        first = max(0, (entry['_due_day'] - start).days)
        for index in range(first+1, 7):
            if compatible(entry, result[index], days[index]) and sum(e['weight'] for e in result[index]) + entry['weight'] <= budgets[index]:
                result[index].append(entry)
                break
    # Gentle smoothing only advances eligible real occurrences by at most two days.
    # Preserve existing/completed entries, all budgets, and room/Sunday exclusions.
    while smoothing and budgets[0] > 0:
        used = sum(e['weight'] for e in result[0])
        options = []
        for index in (1, 2):
            if budgets[index] <= 0:
                continue
            future = sum(e['weight'] for e in result[index])
            for entry in result[index]:
                lead = (entry['_due_day'] - start).days
                if (not entry.get('_smooth_eligible', False) or entry.get('_fixed')
                        or entry.get('_projected') or not 1 <= lead <= 2):
                    continue
                if not compatible(entry, result[0], start) or used + entry['weight'] > budgets[0]:
                    continue
                before = (used / budgets[0]) ** 2 + (future / budgets[index]) ** 2
                after = ((used + entry['weight']) / budgets[0]) ** 2 + ((future - entry['weight']) / budgets[index]) ** 2
                benefit = before - after - 0.035 * lead
                if benefit > 0.025:
                    options.append((benefit, -lead, entry['occurrence_id'], index, entry))
        if not options:
            break
        _, _, _, index, entry = max(options, key=lambda x: x[:3])
        result[index].remove(entry)
        result[0].append(entry)
    allocated = {e['occurrence_id'] for e in result[0]}
    return result, [e for e in unplaced if e['occurrence_id'] not in allocated]
