"""All game rules: access, trick unlocks/costs, door locks and completion.

The sole rule data source is json/rules.json. This module builds access predicates
and evaluates door layouts; Items and Regions supply item identities and graph data.
"""
import json
import pkgutil
from collections import defaultdict
from copy import deepcopy
import typing
from typing import Any

from Options import OptionError
from worlds.generic.Rules import add_rule, set_rule

from .Items import door_keys, item_table, quest_items
from .Options import LockedDoors
from .Regions import all_entrances, locations_by_region, connect_minigame_exits

if typing.TYPE_CHECKING:
    from BaseClasses import CollectionState
    from . import MickeyWorld


# Load once per module, including when installed as a zipped APworld. Callers
# receive a deep copy so one player's rewritten rules cannot affect another.
_RULES = json.loads(pkgutil.get_data(__name__, "json/rules.json"))


def load_rules() -> dict:
    return deepcopy(_RULES)


def order_fill(world: "MickeyWorld", progression: list, locations: list) -> None:
    # Keep AP's shuffled locations except where scarce checks must be saved for
    # the items that open the next route. Sorting every check pushes rewards late.
    remaining = defaultdict(list)
    for item in progression:
        if item.player == world.player:
            remaining[item.name].append(item)
    if not remaining:
        return

    names = list(dict.fromkeys([
        'Star Container', *quest_items,
        *(record['item'] for record in _RULES['tricks'].values()),
        'Mirror Shard', *remaining,
    ]))
    state = world.multiworld.state.copy()
    state.sweep_for_advancements()
    checks = [loc for loc in locations if loc.player == world.player]
    depths = {loc: 0 for loc in checks if loc.can_reach(state)}
    reserved = set()
    ordered = []
    while remaining:
        if len(depths) <= len(ordered) + 1:
            reserved.update(depths)
        best_name, best_state, best_score = None, None, (-1, -1)
        for name in names:
            if name not in remaining:
                continue
            trial = state.copy()
            trial.collect(remaining[name][-1], True)
            trial.sweep_for_advancements()
            score = (sum(loc.can_reach(trial) for loc in checks),
                     len(trial.reachable_regions[world.player]))
            if score > best_score:
                best_name, best_state, best_score = name, trial, score
        state = best_state
        ordered.append(remaining[best_name].pop())
        if not remaining[best_name]:
            del remaining[best_name]
        for loc in checks:
            if loc not in depths and loc.can_reach(state):
                depths[loc] = len(ordered)

    items = iter(ordered)
    progression[:] = [next(items) if item.player == world.player else item
                      for item in progression]
    # Core fills backwards. Protect only the bottleneck checks at the end;
    # the rest retain their random order, including the order within each depth.
    checks = iter([loc for loc in checks if loc not in reserved]
                  + sorted((loc for loc in checks if loc in reserved),
                           key=depths.__getitem__, reverse=True))
    locations[:] = [next(checks) if loc.player == world.player else loc
                    for loc in locations]


def goal_condition(world: "MickeyWorld") -> typing.Callable[["CollectionState"], bool]:
    player = world.player
    goal = _RULES['goal']
    count = getattr(world.options, goal['count_option']).value
    state_gate = _build_single_lambda(_cap_shards(goal['requires'], count), world)
    return lambda state: (state.has(goal['item'], player, count)
                          and state_gate(state)
                          and state.can_reach_region(goal['region'], player))


def _cap_shards(requirement, count):
    if not isinstance(requirement, dict):
        return requirement
    if 'can_reach' in requirement or 'can_complete' in requirement:
        return requirement
    if "has" in requirement:
        term = requirement["has"]
        if isinstance(term, dict) and term["item"] == "Mirror Shard":
            return {"has": {**term, "count": min(term.get("count", 1), count)}}
        return requirement
    return {op: [_cap_shards(child, count) for child in terms]
            for op, terms in requirement.items()}


def impassable_locations() -> frozenset[str]:
    return frozenset(name for name, requirement in _RULES["locations"].items()
                     if requirement is False)


def set_rules(world: "MickeyWorld") -> None:
    world.multiworld.completion_condition[world.player] = goal_condition(world)
    locations, entrances = world.location_requirements, world.entrance_requirements

    for location, requirement in locations.items():
        if location in world.disabled_locations:
            continue
        add_rule(world.get_location(location), _build_single_lambda(requirement, world))

    for entrance, requirement in entrances.items():
        connection = world.multiworld.get_entrance(entrance, world.player)
        add_rule(connection, _build_single_lambda(requirement, world))
        pending = [requirement]
        while pending:
            term = pending.pop()
            if not isinstance(term, dict):
                continue
            if 'can_reach' in term:
                world.multiworld.register_indirect_condition(
                    world.get_region(term['can_reach']), connection)
            pending.extend(term.get('and', ()))
            pending.extend(term.get('or', ()))
    connect_minigame_exits(world)


def compile_requirement(requirement):
    if requirement is True:
        return lambda has, can_reach: True
    if not isinstance(requirement, dict):
        return lambda has, can_reach: False
    for operator in ('or', 'and'):
        if operator in requirement:
            children = tuple(compile_requirement(child) for child in requirement[operator])
            combine = any if operator == 'or' else all
            return lambda has, can_reach: combine(child(has, can_reach) for child in children)
    if 'can_reach' in requirement:
        region = requirement['can_reach']
        if not isinstance(region, str):
            raise ValueError(f'Invalid region requirement: {requirement!r}')
        return lambda has, can_reach: can_reach(region)
    if 'has' in requirement:
        value = requirement['has']
        item = value.get('item', '') if isinstance(value, dict) else value
        count = value.get('count', 1) if isinstance(value, dict) else requirement.get('count', 1)
        if not isinstance(item, str) or type(count) is not int or count < 0:
            raise ValueError(f'Invalid item requirement: {requirement!r}')
        return lambda has, can_reach: has(item, count)
    if 'function' in requirement:
        raise ValueError(f"json/rules.json requires function {requirement['function']!r}, which no rule helper implements")
    return lambda has, can_reach: False


def assign_shuffled_locks(world: "MickeyWorld") -> None:
    doors = {d['lock_index']: d for d in world.locked_doors}
    original = {name: d['lock_index'] for d in world.locked_doors for name in d['entrances']}
    pairs = sorted({tuple(sorted((source, target)))
                    for source, target in world.entrance_connections.items() if source in original})
    owners = {}

    def assign(pair, visited):
        for index in sorted({original[name] for name in pair}):
            if index in visited:
                continue
            visited.add(index)
            if index not in owners or assign(owners[index], visited):
                owners[index] = pair
                return True
        return False

    for pair in pairs:
        if not assign(pair, set()):
            raise ValueError('Shuffled lock pairs do not match their physical door keys')
    keys = set(door_keys.values())
    for index, pair in owners.items():
        for name in pair:
            world.door_locks[name] = index
            requirement = _with_count(_without(world.entrance_requirements.get(name, True), keys),
                                      doors[index]['key_item'], 1)
            world.entrance_requirements[name] = requirement
            set_rule(world.multiworld.get_entrance(name, world.player),
                     _build_single_lambda(requirement, world))


def restrict_secondary_arrivals(world: "MickeyWorld") -> None:
    landings = {name: world.entrance_requirements[name] for name in (
        'Broken Room -> Library', 'Broken Room -> Spa Room', 'Storage Room -> Cave')}
    for source, target in world.entrance_connections.items():
        if target not in landings:
            continue
        room = target.split(' -> ', 1)[0]
        requirement = {'and': [world.entrance_requirements.get(source, True),
                               {'can_reach': room}, landings[target]]}
        world.entrance_requirements[source] = requirement
        connection = world.multiworld.get_entrance(source, world.player)
        set_rule(connection, _build_single_lambda(requirement, world))
        world.multiworld.register_indirect_condition(world.get_region(room), connection)


def evaluate(requirement, counts: typing.Mapping[str, int], regions=()) -> bool:
    return compile_requirement(requirement)(lambda item, count: counts.get(item, 0) >= count,
                                            lambda region: region in regions)


def _build_single_lambda(requirement, world: "MickeyWorld") -> typing.Callable:
    predicate = compile_requirement(requirement)
    player = world.player
    return lambda state: predicate(lambda item, count: state.has(item, player, count),
                                   lambda region: state.can_reach_region(region, player))


def build_requirements(world: "MickeyWorld") -> tuple[dict[str, Any], dict[str, Any]]:
    raw = load_rules()
    locations = dict(raw.get("locations", {}))
    entrances = {name: _cap_shards(req, world.options.shards_required.value)
                 for name, req in raw["entrances"].items()}

    world.trick_costs = _shuffle_trick_costs(world, raw.get("tricks", {}),
                                             locations, entrances)
    add_trick_requirements(world, raw.get('tricks', {}), locations, entrances)
    locations, entrances = _resolve_scene_requirements(locations, entrances)
    world.locked_doors = _choose_locks(
        world, raw.get("doors", []), locations, entrances)
    return locations, entrances


def _resolve_scene_requirements(locations, entrances):
    regions = {loc.name: loc.region for group in locations_by_region.values() for loc in group}

    known_regions = {*locations_by_region, *(e.frm for e in all_entrances),
                     *(e.to for e in all_entrances)}

    def expand(req, visiting=()):
        if not isinstance(req, dict):
            return req
        if 'can_complete' in req:
            name = req['can_complete']
            if name not in regions:
                raise ValueError(f'Unknown scene prerequisite: {name!r}')
            if name in visiting:
                raise ValueError(f'Cycle in scene prerequisites: {(*visiting, name)}')
            return {'and': [{'can_reach': regions[name]},
                            expand(locations.get(name, True), (*visiting, name))]}
        if 'can_reach' in req and req['can_reach'] not in known_regions:
            raise ValueError(f'Unknown region prerequisite: {req!r}')
        if 'has' in req or 'can_reach' in req:
            return req
        return {op: [expand(child, visiting) for child in terms] for op, terms in req.items()}

    return ({name: expand(req, (name,)) for name, req in locations.items()},
            {name: expand(req) for name, req in entrances.items()})

def _shuffle_trick_costs(world: "MickeyWorld", tricks: dict[str, Any],
                         locations: dict, entrances: dict) -> dict[str, int]:
    vanilla = {tid: rec["cost"] for tid, rec in tricks.items()}
    mode = world.options.trick_cost_shuffle
    if mode == mode.option_off:
        return vanilla

    ids = sorted(vanilla)
    sequences = [sequence for rec in tricks.values()
                 for sequence in [rec.get('cost_sequence', []),
                                  *rec.get('location_cost_sequences', {}).values()]]
    while True:
        if mode == mode.option_shuffle:
            # Preserve the vanilla multiset of active trick costs.
            costs = [vanilla[t] for t in ids]
            world.random.shuffle(costs)
            assigned = dict(zip(ids, costs))
        else:
            assigned = {t: world.random.randint(1, 6) for t in ids}
        if all(sum(assigned[t] for t in sequence) <= item_table['Star Container'].frequency
               for sequence in sequences):
            break

    for tid, rec in tricks.items():
        cost = assigned[tid]
        if cost == vanilla[tid]:
            continue
        for name in rec["locations"]:
            if name in locations:
                locations[name] = _set_count(locations[name], "Star Container", cost)
        for name in rec["entrances"]:
            if name in entrances:
                entrances[name] = _set_count(entrances[name], "Star Container", cost)
    return assigned

def _choose_locks(world: "MickeyWorld", doors: list[dict[str, Any]],
                  locations: dict, entrances: dict
                  ) -> list[dict[str, Any]]:
    pool = [d for d in doors if d["pool"]]
    vanilla = [d for d in pool if d["vanilla_locked"]]
    count = world.options.locked_door_count.value

    if world.options.locked_doors == LockedDoors.option_randomized:
        candidates, base = pool, []
    elif count <= len(vanilla):
        candidates, base = vanilla, []
    else:
        base = list(vanilla)
        candidates = [d for d in pool if not d["vanilla_locked"]]
    take = count - len(base)

    attempts = 200
    for _ in range(attempts):
        chosen = base + world.random.sample(candidates, take)
        locs, ents = _apply_locks(doors, chosen, locations, entrances)
        if _solvable(world, locs, ents, chosen):
            locations.clear()
            locations.update(locs)
            entrances.clear()
            entrances.update(ents)
            return chosen
        if take in (0, len(candidates)):
            break  # only one possible set, so retrying samples the same thing

    raise OptionError(
        f"Disney's Magical Mirror ({world.player_name}): could not find a solvable "
        f"layout for {count} locked door(s) in {attempts} attempts. Every layout "
        "tried lacked enough reachable checks for its required keys and tricks. "
        "Lower Locked Door Count or provide the needed starting items.")


def _apply_locks(doors: list[dict[str, Any]],
                 chosen: list[dict[str, Any]], locations: dict,
                 entrances: dict) -> tuple[dict, dict]:
    keys = set(door_keys.values())
    locs, ents = dict(locations), dict(entrances)
    for door in doors:
        for name in door["entrances"]:
            if name in ents:
                stripped = _without(ents[name], keys)
                if stripped is True:
                    ents.pop(name)
                else:
                    ents[name] = stripped
    for door in chosen:
        item = door_keys[door["id"]]
        for name in door["locked_entrances"]:
            ents[name] = _with_count(ents.get(name, True), item, 1)
    return locs, ents


def _solvable(world: "MickeyWorld", locations: dict, entrances: dict,
              chosen: list[dict[str, Any]], targets: dict | None = None) -> bool:
    start = world.start_region_name
    enabled = [loc for region in locations_by_region
               for loc in locations_by_region[region]
               if loc.name not in world.disabled_locations]
    # One matching key per selected door; keys are not interchangeable.
    caps = {"Star Container": 12}
    caps.update({name: item_table[name].frequency for name in quest_items})
    if world.options.tricks:
        caps.update({record['item']: 1 for record in _RULES['tricks'].values()})
    caps['Mirror Shard'] = 12
    all_items = dict(caps)
    for door in chosen:
        item = door_keys[door["id"]]
        all_items[item] = all_items.get(item, 0) + 1

    reached = _sweep(entrances, all_items, start, targets)
    if _RULES['goal']['region'] not in reached:
        return False
    for loc in enabled:
        if loc.region not in reached or not evaluate(locations.get(loc.name, True),
                                                    all_items, reached):
            return False

    frm_of = {entrance.name: entrance.frm for entrance in all_entrances}
    key_sides = {door['id']: [name for name in door['entrances']
                             if name not in world.door_locks] +
                 [name for name, index in world.door_locks.items() if index == door['lock_index']]
                 for door in chosen}
    counts = {item: 0 for item in all_items}
    for item in world.multiworld.precollected_items[world.player]:
        if item.name in counts:
            counts[item.name] = min(counts[item.name] + 1, all_items[item.name])
    starting_items = sum(counts.values())
    while counts != all_items:
        reached = _sweep(entrances, counts, start, targets)
        budget = starting_items + sum(1 for loc in enabled if loc.region in reached
                     and evaluate(locations.get(loc.name, True), counts, reached)) - sum(counts.values())

        if budget <= 0:
            return False
        candidates = set(caps)
        candidates.update(door_keys[door["id"]] for door in chosen
                          if any(frm_of.get(name) in reached for name in key_sides[door['id']]))
        best_item, best_score = None, (-1, -1)
        for item in all_items:
            if item not in candidates or counts[item] >= all_items[item]:
                continue
            trial = dict(counts)
            trial[item] += 1
            regions = _sweep(entrances, trial, start, targets)
            score = (sum(1 for loc in enabled if loc.region in regions
                         and evaluate(locations.get(loc.name, True), trial, regions)), len(regions))
            if score > best_score:
                best_item, best_score = item, score
        if best_item is None:
            return False
        counts[best_item] += 1
    return True


def _sweep(entrances: dict, counts: dict[str, int], start: str,
           targets: dict | None = None) -> set[str]:
    """Regions reachable from `start` while holding `counts`."""
    reached = {start}
    entries, exits = {}, {}
    for door in all_entrances:
        if door.doorway and (sequence := door.doorway.get('minigame')):
            entries[sequence['entrance']] = door.name
            exits[sequence['exit']] = door.name
    while True:
        previous = len(reached)
        for entrance in all_entrances:
            destination = targets.get(exits.get(entrance.name, entrance.name), entrance.to) if targets else entrance.to
            if entrance.frm not in reached or destination in reached:
                continue
            if evaluate(entrances.get(entries.get(entrance.name, entrance.name), True), counts, reached):
                reached.add(destination)
        if len(reached) == previous:
            break
    return reached

def _set_count(req: Any, item: str, count: int) -> Any:
    """Rewrite every `has item` count in `req`. count 0 satisfies it outright."""
    if isinstance(req, dict):
        if 'can_reach' in req or 'can_complete' in req:
            return req
        if "has" in req:
            value = req["has"]
            named = value.get("item") if isinstance(value, dict) else value
            if named == item:
                return True if count <= 0 else {"has": {"item": item, "count": count}}
            return req
        return {op: [_set_count(c, item, count) for c in terms]
                for op, terms in req.items()}
    return req


def _without(req: Any, items: set[str]) -> Any:
    """`req` with every `has` term naming one of `items` satisfied, then simplified."""
    if isinstance(req, dict):
        if 'can_reach' in req or 'can_complete' in req:
            return req
        if "has" in req:
            value = req["has"]
            named = value.get("item") if isinstance(value, dict) else value
            return True if named in items else req
        for op, terms in req.items():
            rebuilt = [_without(c, items) for c in terms]
            if op == "or":
                if any(c is True for c in rebuilt):
                    return True
                rebuilt = [c for c in rebuilt if c is not False]
                if not rebuilt:
                    return False
                return rebuilt[0] if len(rebuilt) == 1 else {"or": rebuilt}
            if op == "and":
                if any(c is False for c in rebuilt):
                    return False
                rebuilt = [c for c in rebuilt if c is not True]
                if not rebuilt:
                    return True
                return rebuilt[0] if len(rebuilt) == 1 else {"and": rebuilt}
            return req
    return req


def _with_count(req: Any, item: str, count: int) -> Any:
    term = {"has": {"item": item, "count": count}}
    if req is True or req is None:
        return term
    return {"and": [req, term]}


def add_trick_requirements(world, registry, locations, entrances):
    def requirements(tid, visiting=()):
        if tid in visiting:
            raise ValueError('Cycle in trick prerequisite registry')
        record = registry[tid]
        terms = []
        if world.options.tricks:
            terms.append({'has': record['item']})
        cost = sum(world.trick_costs[step] for step in record.get('cost_sequence', [tid]))
        if cost:
            terms.append({'has': {'item': 'Star Container', 'count': cost}})
        for parent in record['parents']:
            terms.extend(requirements(parent, (*visiting, tid)))
        return terms

    check_routes = {}
    for tid, record in registry.items():
        terms = requirements(tid)
        for name in record['checks']:
            check_routes.setdefault(name, []).append({'and': terms} if terms else True)
        for mapping, names in ((locations, record['locations']),
                               (entrances, record['entrances'])):
            for name in set(names):
                old = mapping.get(name, True)
                mapping[name] = {'and': [old, *terms]} if terms else old
        for name, sequence in record.get('location_cost_sequences', {}).items():
            cost = sum(world.trick_costs[step] for step in sequence)
            locations[name] = _with_count(locations.get(name, True), 'Star Container', cost)
    for name, routes in check_routes.items():
        route = routes[0] if len(routes) == 1 else {'or': routes}
        locations[name] = {'and': [locations.get(name, True), route]}
